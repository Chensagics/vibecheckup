"""Redaction regression suite.

The dashboard is built to be shared, so anything that identifies its author --
a username, a home directory, an address, a personal or an employer's host --
is a defect in the artifact, not a cosmetic issue. Every case below was
reproduced against the owner's real shipped stats.json before it was fixed;
the docstrings record where each one surfaced.

Runs on synthetic strings and a synthetic corpus only. It never reads
data/, and the end-to-end guard builds its own events file in a temp dir.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from wcstats.clean import (HOSTNAME_TLDS, RE_WINPATH, _drop_line,  # noqa: E402
                          clean_prose)
from wcstats.facets import (FILE_EXTS, RE_EXT,  # noqa: E402
                            error_signature, looks_machine)
from wcstats.tokenize import keep, tokens  # noqa: E402


def toks(text):
    return tokens(clean_prose(text))


# --- 1. `ls -l` output is not prose -----------------------------------------

class TestLongListingIsNotProse(unittest.TestCase):
    """`ls -l` rows read as ordinary words, so the owner and group columns --
    a username -- went through cleaning untouched. In the shipped stats.json
    the phrase "chensagi staff" ranked 61 of 80 in clouds/global/phrases_
    assistant, which the landing tab renders, with n=126; "chensagi" also
    scored as a distinctive term at z=21.36. RE_PATH cannot catch it: the
    username arrives with no slash on it."""

    EXACT = "-rw-r--r--@ 1 chensagi  staff  486 Aug 29 01:04 .gitignore"

    def test_the_exact_row_from_the_audit(self):
        self.assertTrue(_drop_line(self.EXACT))
        self.assertEqual(toks(self.EXACT), [])

    def test_macos_acl_suffixes(self):
        for row in ("-rw-r--r--@ 1 chensagi  staff  486 Aug 29 01:04 .gitignore",
                    "drwxr-xr-x@ 23 chensagi  staff   736 Aug 29 01:20 .",
                    "-rw-rw-r--+ 1 bob  wheel  22 Feb  2 2024 acl.txt"):
            self.assertTrue(_drop_line(row), row)

    def test_linux_selinux_suffix_and_plain_mode(self):
        for row in ("-rw-r--r--. 1 jane jane 1024 Jan  3 09:15 notes.txt",
                    "-rw-r--r-- 1 jane jane 1024 Jan  3 09:15 notes.txt"):
            self.assertTrue(_drop_line(row), row)

    def test_every_file_type_and_permission_bit(self):
        """Symlinks, devices, fifos, sockets, and the setuid/setgid/sticky
        spellings -- all of them carry the same owner column."""
        for row in ("lrwxr-xr-x  1 chensagi  staff   12 Aug 29 link -> target",
                    "crw-rw-rw-  1 root  wheel   3 Aug 29 null",
                    "brw-r-----  1 root  operator 1 Aug 29 disk0",
                    "prw-rw-rw-  1 chensagi  staff   0 Aug 29 fifo",
                    "srwxrwxrwx  1 chensagi  staff   0 Aug 29 sock",
                    "-rwsr-xr-x  1 root  wheel  100 Aug 29 sudo",
                    "-rwxr-sr-x  1 root  wheel  100 Aug 29 setgid",
                    "drwxrwxrwt  1 root  wheel  100 Aug 29 tmp",
                    "-rwSr--r--  1 root  wheel  100 Aug 29 odd"):
            self.assertTrue(_drop_line(row), row)

    def test_indented_rows_are_dropped_too(self):
        self.assertTrue(_drop_line("    " + self.EXACT))

    def test_the_total_header(self):
        for row in ("total 2504", "total 0", "total 4.0K", "total 12M"):
            self.assertTrue(_drop_line(row), row)

    def test_the_word_total_in_a_sentence_survives(self):
        self.assertFalse(_drop_line("total 2504 rows were migrated"))
        self.assertIn("migrated", toks("total 2504 rows were migrated"))

    def test_a_whole_listing_block_yields_no_tokens(self):
        block = ("total 2504\n"
                 "drwxr-xr-x@ 23 chensagi  staff   736 Aug 29 01:20 .\n"
                 "-rw-r--r--@  1 chensagi  staff   486 Aug 29 01:04 .gitignore\n"
                 "-rw-r--r--@  1 chensagi  staff 15737 Aug 29 01:20 analyze.py\n")
        self.assertEqual(toks(block), [])

    def test_prose_around_a_listing_survives(self):
        got = toks("please review the listing below\n" + self.EXACT
                   + "\nand tell me which files are stale")
        self.assertIn("review", got)
        self.assertIn("stale", got)
        self.assertNotIn("chensagi", got)
        self.assertNotIn("staff", got)


# --- 2. Windows and UNC paths ------------------------------------------------

class TestWindowsPathsAreMasked(unittest.TestCase):
    """RE_PATH and the error-signature path mask were both forward-slash only.
    The README supports Windows through WSL and Git Bash, where `C:\\Users\\
    <name>` is exactly what tool output prints, so a Windows user's username
    and client folder names went into the clouds and the errors facet
    verbatim."""

    PROSE = r"fix C:\Users\jane.doe\Projects\acme-client\src\app.ts"
    ERROR = r"ENOENT C:\Users\jane.doe\Projects\acme\secret.env not found"

    def test_the_exact_prose_case_from_the_audit(self):
        got = toks(self.PROSE)
        self.assertIn("fix", got)
        for leaked in ("users", "jane.doe", "projects", "acme-client", "app.ts"):
            self.assertNotIn(leaked, got)

    def test_the_exact_error_case_from_the_audit(self):
        sig = error_signature(self.ERROR)
        self.assertEqual(sig, "ENOENT PATH not found")
        for leaked in ("jane.doe", "Users", "acme", "secret.env"):
            self.assertNotIn(leaked, sig)

    def test_unc_share(self):
        text = r"audit \\fileserver\share\Clients\AcmeCorp\budget.xlsx today"
        got = toks(text)
        self.assertEqual(got, ["audit", "today"])
        self.assertNotIn("AcmeCorp", error_signature(
            r"Error: cannot open \\nas01\share\Private\AcmeCorp\keys.pem"))

    def test_lowercase_drive_and_forward_slash_spelling(self):
        self.assertNotIn("secret-project", toks(r"d:\work\secret-project\x.go"))
        self.assertNotIn("secret-project", toks("d:/work/secret-project/x.go"))

    def test_a_bare_drive_root_goes_too(self):
        self.assertEqual(clean_prose(r"cd C:\ then build"), "cd then build")

    def test_a_colon_slash_inside_a_word_is_not_a_drive(self):
        """The lookbehind keeps `note:/tmp/x` and a leftover `s://` from
        being read as a drive letter and eating the word in front of them."""
        self.assertIsNone(RE_WINPATH.search("note:/tmp/x"))
        self.assertIsNone(RE_WINPATH.search("https://example.com/a"))
        self.assertIn("ratio", toks("the ratio was 3:1 across builds"))

    def test_both_modules_mask_the_same_shapes(self):
        """clean.py and facets.py share one regex on purpose -- a Windows
        path masked in the clouds but not in the errors facet is still a
        leak, and two copies of a rule drift."""
        for path in (r"C:\Users\jane\x.ts", r"\\host\share\x.ts",
                     "C:/Users/jane/x.ts"):
            self.assertTrue(RE_WINPATH.search(path), path)
            self.assertNotIn("jane", clean_prose("open " + path))
            self.assertNotIn("jane", error_signature("Error: cannot open " + path))


# --- 3. Addresses and hostnames are not vocabulary --------------------------

class TestAddressesAndHostnames(unittest.TestCase):
    """The tokenizer allows `.` and `-` inside a token, so hostnames became
    cloud words. Shipped in the owner's stats.json: `mejanreteam.chensagi.com`
    (a personal host) in by_project/mae/prose_user, and `izs.me` -- a THIRD
    PARTY's email domain -- promoted into tripoli/distinctive_user."""

    def test_the_exact_hostnames_from_the_audit(self):
        got = toks("mail ops@acme-corp.com about mejanreteam.chensagi.com "
                   "and izs.me")
        self.assertEqual(got, ["mail"])
        for leaked in ("acme-corp.com", "mejanreteam.chensagi.com", "izs.me",
                       "ops"):
            self.assertNotIn(leaked, got)

    def test_third_party_service_hosts(self):
        got = toks("check grok.com and myinstants.com for the sound")
        self.assertNotIn("grok.com", got)
        self.assertNotIn("myinstants.com", got)
        self.assertIn("sound", got)

    def test_an_address_is_removed_local_part_and_all(self):
        """Rejecting the domain half is not enough -- `jane.doe` on its own
        names a person just as well."""
        got = toks("user jane.doe@example.co.uk replied")
        self.assertEqual(got, ["replied"])

    def test_keep_rejects_addresses_directly(self):
        self.assertFalse(keep("ops@acme-corp.com"))
        self.assertFalse(keep("jane.doe@example.com"))

    def test_keep_rejects_hostname_shapes(self):
        for host in ("izs.me", "grok.com", "sec.gov", "claude.ai",
                     "mejanreteam.chensagi.com", "api.github.com",
                     "com.chencorp.finn", "a.b.c"):
            self.assertFalse(keep(host), host)

    def test_legitimate_dotted_tokens_still_pass(self):
        """The TLD list is curated around this: ccTLDs that are also everyday
        file extensions are deliberately absent, so source files keep their
        names."""
        for word in ("app.py", "server.ts", "main.rs", "node.js", "readme.md",
                     "build.sh", "lib.rs", "index.php", "run.pl"):
            self.assertTrue(keep(word), word)

    def test_the_shapes_the_existing_tokenizer_tests_pin(self):
        """test_i18n_time.TestAsciiBehaviourUnchanged and TestTokenize both
        depend on these; the hostname rule must not touch them."""
        from wcstats.tokenize import raw_tokens
        got = raw_tokens("write it in c++ and read the .py file, "
                         "don't forget my e-mail")
        for shape in ("c++", "py", "don't", "e-mail"):
            self.assertIn(shape, got)
        self.assertTrue(keep("e-mail"))
        self.assertTrue(keep("python3.11"))       # version-ish, one dot
        self.assertIn("deploy", toks("Please just fix the failing deploy test"))
        self.assertIn("función", tokens("revisa la función del módulo"))

    def test_the_tld_list_holds_no_common_file_extension(self):
        """One entry too many here silently deletes a language from every
        cloud -- `.py`, `.ts` and `.rs` are all ccTLDs."""
        for ext in ("py", "ts", "rs", "sh", "md", "pl", "so", "cs", "ml",
                    "in", "el", "pm", "cc", "js", "go", "rb", "java"):
            self.assertNotIn(ext, HOSTNAME_TLDS, ext)


# --- 3b. LAN hostnames ------------------------------------------------------

class TestSpecialUseHostnamesAreMasked(unittest.TestCase):
    """The TLD list held every public suffix and not one of the ones a LAN
    actually uses, so the machine's own name survived cleaning whole.

    `hostname` on every Mac answers `<account>-MacBook-Pro.local`, and that
    string rides along in ssh banners, mDNS failures and a dev server's
    "Network:" line -- which is to say, the account name reaching the clouds
    with no redactor installed at all.
    """

    HOSTS = ("janedoe-macbook-pro.local", "buildbox.internal", "nas.lan",
             "gitlab.corp", "printer.home", "db.localdomain", "wiki.intranet",
             "vault.private", "1.0.0.127.in-addr.arpa", "dev.localhost")

    def test_the_exact_case_from_the_audit(self):
        got = toks("ssh into janedoe-macbook-pro.local for the logs")
        self.assertNotIn("janedoe-macbook-pro.local", got)
        self.assertNotIn("janedoe", " ".join(got))
        self.assertIn("logs", got)

    def test_every_special_use_suffix(self):
        for host in self.HOSTS:
            with self.subTest(host=host):
                self.assertFalse(keep(host))
                self.assertNotIn(host, toks("point it at " + host + " today"))

    def test_the_bare_words_are_still_vocabulary(self):
        """The rule matches a DOTTED SUFFIX. `local`, `home`, `private`,
        `internal` and `corp` are ordinary words and must survive."""
        got = toks("keep the local copy at home on a private internal corp lan "
                   "host so the arpa intranet stays offline")
        for word in ("local", "home", "private", "internal", "corp", "lan",
                     "host", "arpa", "intranet"):
            self.assertIn(word, got, word)

    def test_no_common_file_extension_was_added(self):
        """`Button.test`, `fixture.example` and `x.invalid` are filename stems,
        so RFC 6761's other reserved names are deliberately left out."""
        for ext in ("test", "example", "invalid", "spec", "min", "map",
                    "lock", "bak", "orig"):
            self.assertNotIn(ext, HOSTNAME_TLDS, ext)
        self.assertTrue(keep("button.test"))

    def test_error_signatures_inherit_the_same_list(self):
        """facets.RE_MASK_HOST is built from HOSTNAME_TLDS, so a signature
        that names the machine has to mask it too."""
        sig = error_signature("Error: connect ECONNREFUSED "
                              "janedoe-macbook-pro.local:8080")
        self.assertNotIn("janedoe", sig)
        self.assertIn("HOST", sig)


# --- 3c. Secrets are not vocabulary -----------------------------------------

class TestHighEntropyTokensAreRejected(unittest.TestCase):
    """clean_prose removes blobs by shape, and its rules left a gap exactly
    the width of a modern API key: RE_B64 wants 40+ characters and rejects
    base64url's `-`/`_`, RE_HEX wants 12+ hex and nothing else, and keep()
    only caps a token at 28.

    `sk-proj-Ab3xQ9pLm2` came out of the tokenizer as
    `['key', 'sk-proj-ab3xq9plm2']`, and 202 opaque ids from Antigravity's
    protobuf recovery (`eyp7apy0npczxn8p1l-xqqe`) sat in the shipped
    vocab.json.
    """

    OPAQUE = ("sk-proj-ab3xq9plm2", "eyp7apy0npczxn8p1l-xqqe",
              "q1f6arrfi6uc7m8p75et8aw", "rlf6atgmfncrkdup8kqjiag",
              "e97asfbhpqp28opm5v3ka0", "a0o4atrgbjpz7m8pqszyyak",
              "acakatkubohe7m8pjoo3-aq", "alt6avuhmnz4xn8pqs3wiqe")

    CREDENTIALS = ("sk-proj-ab3xq9plm2", "ghp_abcdefghij1234567890",
                   "github_pat_11abcdefgh", "glpat-abcdefgh12345",
                   "xoxb-abcdefghij", "akiaiosfodnn7example",
                   "aizasybcdefghijklmnop", "dop_v1_abcdefgh12",
                   "shpat_abcdefgh1234", "pypi-agendhjklzxcv")

    def test_the_exact_case_from_the_audit(self):
        self.assertEqual(toks("key sk-proj-Ab3xQ9pLm2"), ["key"])
        self.assertEqual(toks("blob eyp7apy0npczxn8p1l-xqqe"), ["blob"])

    def test_opaque_ids_are_rejected(self):
        for tok in self.OPAQUE:
            with self.subTest(tok=tok):
                self.assertFalse(keep(tok))

    def test_named_credential_shapes_are_rejected(self):
        """A vendor prefix is a stronger signal than any entropy measure, and
        it catches the ones entropy cannot: `AKIA` + 16 characters carries a
        single digit run."""
        for tok in self.CREDENTIALS:
            with self.subTest(tok=tok):
                self.assertFalse(keep(tok))

    def test_a_numeronym_is_not_a_secret(self):
        """`i18n`, `a11y`, `l10n` and a version suffix all have exactly ONE
        digit run; a random id has them scattered. `i18nmanager` alone is 53
        occurrences of real vocabulary in the owner's corpus."""
        for tok in ("i18nmanager", "i18nmanager.isrtl", "initreacti18next",
                    "confirma11ylabel", "compute20daylow", "css1compat",
                    "migratefromv4tomultisession", "get20daylow"):
            with self.subTest(tok=tok):
                self.assertTrue(keep(tok), tok)

    def test_ordinary_filenames_survive(self):
        for tok in ("missing_strings_0.json", "components_group_2_he.json",
                    "game_mockup_v2.html", "wilhelm_scream.mp3",
                    "compiled.pt-br.chunk1.json", "tournament_v2_schema.sql",
                    "sell-toast-cfge-plus448.png"):
            with self.subTest(tok=tok):
                self.assertTrue(keep(tok), tok)

    def test_words_that_merely_start_like_a_credential_survive(self):
        for tok in ("sk-learn", "skeleton", "pk-anything", "skip-list",
                    "npm_package_version", "hf_hub", "ghost-writer"):
            with self.subTest(tok=tok):
                self.assertTrue(keep(tok), tok)


# --- 4. Error signatures carry no author-specific text ----------------------

class TestErrorSignatureCarriesNoIdentity(unittest.TestCase):
    """A signature is the shape of a failure, so free-form text riding along
    is a bug. The dashboard renders these under "What went wrong most" and in
    every facet's errors dropdown. All four cases below are verbatim from the
    owner's shipped stats.json."""

    def test_account_name_in_an_owner_slug(self):
        """Shipped as "...for chensagiPATH: Vercel API error N" -- the POSIX
        mask ate `/vibecheckup` and left the Vercel ACCOUNT NAME behind."""
        sig = error_signature(
            'Failed to create project "vibecheckup" for chensagi/vibecheckup: '
            "Vercel API error 403")
        self.assertNotIn("chensagi", sig)
        self.assertEqual(sig, "Failed to create project 'X' for PATH: "
                              "Vercel API error N")

    def test_relative_paths_lose_their_first_segment_too(self):
        self.assertEqual(
            error_signature("rg: src/components/Card.tsx: No such file"),
            "rg: PATH: No such file")

    def test_short_word_pairs_are_not_mistaken_for_owner_slugs(self):
        """Both sides of the new relative-path rule need three characters, so
        it cannot fire on `and/or` or `24/7`. (The pre-existing POSIX rule
        still masks a leading slash wherever it finds one -- that behaviour is
        unchanged and is not what this rule is for.)"""
        from wcstats.facets import RE_MASK_RELPATH
        for pair in ("and/or", "N/A", "24/7", "km/h"):
            self.assertIsNone(RE_MASK_RELPATH.search(pair), pair)
        self.assertTrue(RE_MASK_RELPATH.search("chensagi/vibecheckup"))

    def test_commit_subjects_are_masked(self):
        """git prints the author's own commit message as part of a rebase
        failure; two of these shipped whole."""
        for msg, in (
            ("error: could not apply 45c3e2fad... feat: upgrade home and academy visuals",),
            ("error: could not apply 242fe0f81... docs: add asset generation handoff plan",),
            ("error: could not apply 5ca2f39f9... Finalize lesson tasks and fix lint error",),
        ):
            sig = error_signature(msg)
            self.assertEqual(sig, "error: could not apply HEX... MSG", msg)

    def test_identical_failures_still_group(self):
        """Masking is worthless if it splits one failure into many, or fuses
        two different ones."""
        a = error_signature("error: could not apply aaaaaaaaa... feat: one thing")
        b = error_signature("error: could not apply bbbbbbbbb... feat: another")
        self.assertEqual(a, b)
        self.assertNotEqual(a, error_signature("error: cannot open PATH"))

    def test_hostnames_in_errors(self):
        self.assertEqual(error_signature("error connecting to api.github.com"),
                         "error connecting to HOST")

    def test_bundle_ids_name_an_organisation(self):
        sig = error_signature("Error terminating app: Command failed: "
                              "xcrun simctl terminate 1234 com.chencorp.finn")
        self.assertNotIn("chencorp", sig)

    def test_addresses_and_handles(self):
        self.assertIn("EMAIL", error_signature("smtp auth failed for ops@acme.com"))
        self.assertIn("@USER", error_signature("error: @chensagics is not a collaborator"))

    def test_only_the_first_sentence_is_kept(self):
        """A vendor's paragraph and a trailing working directory are both
        free-form text the signature has no use for."""
        self.assertEqual(
            error_signature("This ad account is not enabled for the Ads MCP. "
                            "Ads MCP is being gradually rolled out."),
            "This ad account is not enabled for the Ads MCP.")
        self.assertEqual(
            error_signature("File does not exist. Note: your current working "
                            "directory is /Users/chensagi/Projects/keep"),
            "File does not exist.")

    def test_an_ellipsis_is_not_a_sentence_boundary(self):
        self.assertIn("MSG", error_signature(
            "error: could not apply aaaaaaaaa... Fix the thing"))

    def test_ansi_escapes_do_not_survive(self):
        self.assertEqual(
            error_signature("\x1b[1;31mSCRIPT ERROR:\x1b[0;31m Parse failed"),
            "SCRIPT ERROR: Parse failed")

    def test_the_original_volatility_contract_still_holds(self):
        self.assertEqual(error_signature("Error: cannot open /Users/a/x.py line 42"),
                         error_signature("Error: cannot open /Users/b/y.py line 99"))

    def test_empty_and_prose_input(self):
        self.assertEqual(error_signature(""), "")
        self.assertEqual(error_signature("   \n  "), "")


# --- 4b. The errors facet holds error signatures, not sentences -------------

class TestOnlyMachineOutputBecomesASignature(unittest.TestCase):
    """The line was chosen by a substring scan for error|exception|failed|
    fatal|traceback|cannot|not found|permission denied|refused, anywhere in
    the line, over the whole body of any flagged event. Those are ordinary
    English words, so the shipped dashboard.html published under "What went
    wrong most": a product-curriculum sentence, an agent-doctrine table row, a
    skill's frontmatter `description:`, a JSON issue title, four lines of the
    owner's own source, a .gitignore entry and a git push confirmation.

    A keyword now only counts where a machine puts one.
    """

    PROSE = (
        # the product curriculum, matched on "cannot"
        "2. **The stop-loss** - define risk, accept being wrong. "
        "*(If you cannot lose small, you cannot stay in the game.)*",
        # an agent-doctrine table row, matched on "exception"
        "| **Exclusive-resource round** | testers sharing ONE exclusive "
        "device run **sequentially** under its lock (the sanctioned "
        "exception to doctrine) |",
        # a source comment
        "* These tests catch common configuration errors before they "
        "reach production.",
        # a skill's frontmatter
        "description: Debug and fix runtime errors by screenshotting the "
        "simulator, reading logs, and iterating",
        # an agent instruction
        "Spawn a cheap Haiku agent to collect current errors:",
        "Standardizes command building and device targeting to prevent errors.",
        # a founder directive carried in a roadmap row
        "compiles it to a lesson. This is a **bounded exception to the "
        "single-source rule**",
        # source code
        "if (errors.length) {",
        "const accentColor = (a === 'buy') ? colors.primary : colors.error;",
        'const COL_ERROR: Color = Color("d68670")',
        "console.error('No available session slots');",
        # a .gitignore line and git plumbing output
        "yarn-error.log*",
        "* [new branch]      claude/fix-render-error-UgUfz -> origin/x",
        "[feat/more-lessons 88547d5] Finalize lesson tasks and fix lint error",
        "1324a65 Finalize lesson tasks and fix lint error",
        # JSON keys and report summaries
        '"errors": []',
        "- gaps: 88 (errors: 88, warnings: 0)",
        '"result": "0 magnitude errors, 216 baselined entries"',
    )

    MACHINE = (
        "Traceback (most recent call last):",
        "TypeError: Cannot read properties of undefined (reading 'dump')",
        "SyntaxError: unterminated string literal (detected at line 7)",
        "fatal: not a git repository (or any of the parent directories): .git",
        "error: cannot pull with rebase: You have unstaged changes.",
        "npm error code ENOTFOUND",
        "rg: metro.config.ts: No such file or directory (os error 2)",
        "(eval):1: command not found: shopt",
        "<tool_use_error>File has not been read yet.</tool_use_error>",
        "42:  [ERROR] Magnitude baseline: count=151 does not match 152 entries",
        "9:34  error  'useMemo' is defined but never used.",
        "src/x.test.ts(57,21): error TS2554: Expected 5 arguments, but got 4.",
        "SCRIPT ERROR: Parse Error: Assignment is not allowed here.",
        "awk: syntax error at source line 1",
        "curl: (22) The requested URL returned error: 404",
        "PreToolUse:Bash hook error: [$DIR/guard.py]: BLOCKED (fleet hook)",
        "Failed to fetch document content at https://example.com/x",
        "load-app: FAILED to reach game content in 90s",
        "lsof: status error on 8772: No such file or directory",
        "Error: Operation not permitted @ dir_s_mkdir - /tmp/x",
        "ParserError while parsing a block mapping",
    )

    # Every prose line above, buried in the kind of body it really arrived in:
    # a document the agent read, flagged as an error event because the tool
    # call failed. Nothing in it is a diagnostic, so nothing in it is a
    # signature -- which is the contract the old substring scan broke.
    DOC_HEAD = ("# Notes\n\nSome ordinary paragraph that mentions nothing "
                "unusual at all.\n\n")

    def test_a_prose_line_is_never_selected(self):
        for line in self.PROSE:
            with self.subTest(line=line[:48]):
                self.assertFalse(looks_machine(line), line[:60])

    def test_a_prose_line_never_becomes_a_signature(self):
        for line in self.PROSE:
            with self.subTest(line=line[:48]):
                self.assertEqual(error_signature(self.DOC_HEAD + line), "",
                                 line[:60])

    def test_a_real_diagnostic_line_is_selected(self):
        for line in self.MACHINE:
            with self.subTest(line=line[:48]):
                self.assertTrue(looks_machine(line), line[:60])

    def test_a_real_diagnostic_still_becomes_a_signature(self):
        for line in self.MACHINE:
            with self.subTest(line=line[:48]):
                self.assertNotEqual(error_signature(self.DOC_HEAD + line), "",
                                    line[:60])

    def test_the_exact_document_from_the_audit_yields_nothing(self):
        """The curriculum sentence did not arrive alone -- it was one line of
        a document the agent had read, flagged as an error event."""
        doc = ("# Day trading, week 2\n\n"
               "The four rules of the desk:\n\n"
               "1. **Position sizing** - never more than 1R.\n"
               "2. **The stop-loss** - define risk, accept being wrong. "
               "*(If you cannot lose small, you cannot stay in the game.)*\n"
               "3. **The journal** - write the thesis before the entry.\n")
        self.assertEqual(error_signature(doc), "")

    def test_a_diagnostic_buried_under_a_status_line_still_wins(self):
        """`Exit code 1` heads the body and the real failure is ten lines
        down, so the two are not in reading order: a single first-match pass
        collapsed 17 distinct signatures onto "Exit code N"."""
        body = ("Exit code 1\n"
                "\n"
                "> typecheck\n"
                "> tsc --noEmit\n"
                "\n"
                "src/game/audio.ts(275,33): error TS7053: Element implicitly "
                "has an 'any' type.\n")
        self.assertIn("TS", error_signature(body))
        self.assertNotIn("Exit code", error_signature(body))

    def test_a_status_line_is_still_a_signature_on_its_own(self):
        self.assertEqual(error_signature("Exit code 1"), "Exit code N")

    def test_the_harness_refusals_survive(self):
        """Plain-English refusals with no marker in them at all are the
        largest single group in the real corpus, and they are real failures.
        They are short, which is what separates them from a document."""
        for line in ("This session is isolated in the worktree /a/b/c, but "
                     "this command is too complex to verify.",
                     "The user doesn't want to proceed with this tool use.",
                     "Permission for this action was denied by the auto mode "
                     "classifier."):
            with self.subTest(line=line[:40]):
                self.assertNotEqual(error_signature(line), "")

    def test_a_long_document_with_no_marker_yields_nothing(self):
        doc = "\n".join(f"line {i} of an ordinary design note" for i in range(60))
        self.assertEqual(error_signature(doc), "")

    def test_a_non_zero_exit_code_attests_a_long_body(self):
        """The caller may know something the text does not say."""
        doc = "\n".join(f"line {i} of an ordinary design note" for i in range(60))
        self.assertEqual(error_signature(doc, exit_code=0), "")
        self.assertNotEqual(error_signature(doc, exit_code=1), "")

    def test_source_lines_are_never_the_signature(self):
        """A grep hit and a stack-trace preamble arrive inside a tool result.
        `throw err;` used to outrank the `Error: Cannot find module` two lines
        below it."""
        body = ("node:internal/modules/cjs/loader:1215\n"
                "  throw err;\n"
                "  ^\n"
                "Error: Cannot find module 'sharp'\n")
        self.assertIn("Cannot find module", error_signature(body))


# --- 5. A file type is a file's extension, not a domain's last label --------

class TestFileTypeIsNotADomain(unittest.TestCase):
    """RE_EXT read the last label of the trailing whitespace token, so bare
    hostnames landed in the dashboard's "File types touched" cloud. Shipped in
    the owner's stats.json beside `py` and `ts`: `etoro` (a COMPANY NAME, from
    `help.etoro`), `com` (`grok.com`, `bearbulltraders.com`), `me`
    (`grok.me`), and `finn` (`com.chencorp.finn`)."""

    def ext(self, candidate):
        m = RE_EXT.search(candidate)
        return m.group(1).lower() if m else None

    def test_the_exact_candidates_from_the_corpus(self):
        for candidate in ("help.etoro", "grok.com", "grok.me",
                          "site:bearbulltraders.com", "com.chencorp.finn",
                          "com.chensagi.finn", "sec.gov", "izs.me"):
            self.assertIsNone(self.ext(candidate), candidate)

    def test_urls_are_never_file_types(self):
        for candidate in ("https://www.etoro.com/help.etoro",
                          "http://example.com/x.php", "file:///c/x.md"):
            self.assertIsNone(self.ext(candidate), candidate)

    def test_bare_filenames_with_known_extensions_still_count(self):
        for candidate, want in (("main.py", "py"), ("README.md", "md"),
                                ("App.tsx", "tsx"), ("scene.tscn", "tscn"),
                                ("Makefile.mk", "mk"), ("shot.PNG", "png")):
            self.assertEqual(self.ext(candidate), want, candidate)

    def test_a_path_vouches_for_an_unknown_extension(self):
        """`.output`, `.example` and `.maestro` are all real in this corpus and
        all arrive on a path, which is what makes them trustworthy."""
        for candidate, want in (
                ("/Users/chensagi/Projects/keep/local.mk.example", "example"),
                ("a/b/tasks/bjnjsspwb.output", "output"),
                ("/Users/chensagi/projects/finn/x.maestro", "maestro"),
                ("src/app.tsx", "tsx"),
                (r"C:\work\app.ts", "ts")):
            self.assertEqual(self.ext(candidate), want, candidate)

    def test_a_dotfile_is_not_a_file_type(self):
        self.assertIsNone(self.ext(".agents"))

    def test_the_allowlist_entries_all_fit_the_captured_shape(self):
        """An entry longer than seven characters can never match, so it would
        sit in the list looking effective and do nothing."""
        import re
        shape = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,6}$")
        for ext in FILE_EXTS:
            self.assertRegex(ext, shape, ext)
        self.assertEqual(len(FILE_EXTS), len(set(FILE_EXTS)),
                         "duplicate entries in FILE_EXTS")

    def test_no_allowlisted_extension_is_a_hostname_tld(self):
        """The two lists have opposite jobs; an entry in both makes the
        outcome depend on which rule runs first."""
        self.assertEqual(set(FILE_EXTS) & set(HOSTNAME_TLDS), set())


# --- the end-to-end guard ----------------------------------------------------

class TestSyntheticCorpusShipsNoIdentity(unittest.TestCase):
    """One broad guard over the real pipeline: build an events file that
    contains every leak above, run analyze.py on it, and read the stats.json
    it produces. Unit rules can each be right while the pipeline still ships
    the string -- this is the test that would have caught all five."""

    IDENTITY = ("chensagi", "jane.doe", "acme-corp.com", "izs.me",
                "mejanreteam.chensagi.com", "grok.com", "myinstants.com",
                "etoro", "chencorp", "AcmeCorp", "fileserver",
                "ops@acme-corp.com", "Users", "secret-project")

    LS_BLOCK = (
        "total 2504\n"
        "drwxr-xr-x@ 23 chensagi  staff   736 Aug 29 01:20 .\n"
        "-rw-r--r--@  1 chensagi  staff   486 Aug 29 01:04 .gitignore\n"
        "-rw-r--r--.  1 chensagi  staff 15737 Aug 29 01:20 analyze.py\n")

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="vibecheckup-redaction-")
        events = os.path.join(cls.tmp, "events.ndjson")
        out = os.path.join(cls.tmp, "stats.json")
        rows = []

        def ev(**kw):
            base = {"tool": "claude_code", "session_id": "s1",
                    "ts": "2026-08-01T12:00:00+00:00", "project": "demo",
                    "role": "user", "kind": "prompt", "text": ""}
            base.update(kw)
            rows.append(base)

        # Repeated so the terms clear every min_count floor downstream.
        for i in range(40):
            ev(session_id="s%d" % (i % 5),
               text="please audit the deploy pipeline\n" + cls.LS_BLOCK)
            ev(role="assistant", kind="reply", session_id="s%d" % (i % 5),
               text="here is the listing you asked for\n" + cls.LS_BLOCK)
            ev(session_id="s%d" % (i % 5),
               text=r"fix C:\Users\jane.doe\Projects\acme-client\src\app.ts "
                    r"and \\fileserver\share\Clients\AcmeCorp\budget.xlsx "
                    r"and d:\work\secret-project\main.go")
            ev(role="assistant", kind="reply", session_id="s%d" % (i % 5),
               text="mail ops@acme-corp.com about mejanreteam.chensagi.com, "
                    "izs.me, grok.com and myinstants.com")
            ev(role="tool", kind="tool_call", tool_name="Read",
               session_id="s%d" % (i % 5), text="help.etoro")
            ev(role="tool", kind="tool_call", tool_name="Read",
               session_id="s%d" % (i % 5), text="com.chencorp.finn")
            ev(role="tool", kind="error", session_id="s%d" % (i % 5),
               text="Failed to create project \"app\" for chensagi/vibecheckup: "
                    "Vercel API error 403")
            ev(role="tool", kind="error", session_id="s%d" % (i % 5),
               text="error: could not apply 45c3e2fad... feat: upgrade the "
                    "home and academy visuals")
            ev(role="tool", kind="error", session_id="s%d" % (i % 5),
                text=r"ENOENT C:\Users\jane.doe\Projects\acme\secret.env not found")

        with open(events, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

        proc = subprocess.run(
            [sys.executable, os.path.join(ROOT, "analyze.py"),
             "--in", events, "--out", out,
             "--vocab", os.path.join(cls.tmp, "vocab.json")],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise AssertionError("analyze.py failed: " + proc.stderr[-2000:])
        with open(out, encoding="utf-8") as fh:
            cls.blob = fh.read()
        cls.S = json.loads(cls.blob)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_no_identity_string_anywhere_in_the_stats(self):
        for needle in self.IDENTITY:
            self.assertNotIn(needle, self.blob, "%r reached stats.json" % needle)

    def test_the_listing_columns_are_not_vocabulary(self):
        for facet in ("prose_user", "prose_assistant", "prose_user_raw"):
            terms = {d["t"] for d in self.S["clouds"]["global"][facet]}
            self.assertEqual(terms & {"chensagi", "staff", "gitignore"}, set(),
                             facet)

    def test_no_phrase_pairs_the_owner_with_the_group(self):
        for facet in ("phrases_user", "phrases_assistant"):
            for d in self.S["clouds"]["global"][facet]:
                self.assertNotIn("chensagi", d["t"], facet)
                self.assertNotIn("staff", d["t"].split(), facet)

    def test_the_prose_around_the_noise_survived(self):
        """A redaction that empties the corpus is not a fix."""
        terms = {d["t"] for d in self.S["clouds"]["global"]["prose_user"]}
        self.assertIn("audit", terms)
        self.assertIn("pipeline", terms)

    def test_no_domain_became_a_file_type(self):
        exts = {d["t"] for d in self.S["clouds"]["global"]["extensions"]}
        self.assertEqual(exts & {"etoro", "com", "me", "finn"}, set())

    def test_error_signatures_carry_no_account_name_or_commit_subject(self):
        sigs = " || ".join(d["t"] for d in self.S["clouds"]["global"]["errors"])
        self.assertNotIn("chensagi", sigs)
        self.assertNotIn("academy", sigs)
        self.assertNotIn("jane.doe", sigs)
        self.assertIn("PATH", sigs)

    def test_the_absolute_path_contract_covers_windows_too(self):
        """test_all.TestStatsContract asserts this over the owner's real
        stats.json; assert it here over a corpus built to violate it."""
        import re
        found = re.findall(r"(?:/Users/|/home/|[A-Za-z]:\\\\)[^\"\\\\ ,]{2,80}",
                           self.blob)
        self.assertEqual(found, [], "absolute paths in stats.json: %r" % found)


if __name__ == "__main__":
    unittest.main(verbosity=2)
