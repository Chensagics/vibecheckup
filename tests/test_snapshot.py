"""snapshot.py: the redaction-schema stamp, and flagging stale snapshots.

A dated copy of a dashboard is exactly a hand-over artifact, and it outlives
the code that built it. Whatever a build let through, the snapshot keeps
forever -- so the file has to say which version of the redaction rules made it,
and `--list` has to say so out loud when that version is behind.

Runs on synthetic pages in a temporary directory. It never reads or writes the
real snapshots/, dashboard.html or data/.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import build_dashboard as bd      # noqa: E402
import snapshot as sn             # noqa: E402

PAGE = "<!DOCTYPE html>\n<html lang=\"en\">\n<p>hi</p>\n</html>\n"


class TestTheStamp(unittest.TestCase):

    def test_a_built_page_carries_the_current_schema(self):
        self.assertEqual(bd.marker_version(bd.stamp(PAGE)), bd.REDACTION_SCHEMA)

    def test_the_stamp_leads_the_page_but_follows_the_doctype(self):
        """Quirks mode is a doctype away, and --list only reads the head."""
        out = bd.stamp(PAGE)
        self.assertTrue(out.startswith('<!DOCTYPE html>\n<html lang="en">'))
        self.assertLess(out.index(bd.MARKER), bd.HEAD_BYTES)
        self.assertLess(out.index(bd.MARKER), out.index("<p>hi</p>"))

    def test_a_bare_fragment_is_still_stamped(self):
        """A template edit must not silently drop the marker."""
        self.assertEqual(bd.marker_version(bd.stamp("<p>fragment</p>")),
                         bd.REDACTION_SCHEMA)

    def test_an_unmarked_page_reads_as_zero(self):
        self.assertEqual(bd.marker_version(PAGE), 0)
        self.assertEqual(bd.marker_version(""), 0)
        self.assertEqual(bd.marker_version(None), 0)

    def test_log_text_cannot_forge_a_version(self):
        """render() escapes every "<", so a "<!--" cannot occur in the payload.
        A user who typed the marker word at their agent must not be able to
        pass an old page off as a current one."""
        stats = {"schema_version": 2, "clouds": {}, "coverage": {},
                 "totals": {}, "activity": {},
                 "note": f"<!-- {bd.MARKER}: 99 -->"}
        with open(os.path.join(ROOT, "dashboard_template.html"),
                  encoding="utf-8") as fh:
            html = bd.render(json.dumps(stats), fh.read())
        self.assertEqual(bd.marker_version(html), 0)
        self.assertEqual(bd.marker_version(bd.stamp(html)), bd.REDACTION_SCHEMA)


class TestListFlagsStaleSnapshots(unittest.TestCase):
    """The four leak shapes the audit found were all built by an older
    pipeline. The snapshots holding them are still on disk and still readable,
    so the listing is the only place left to say what they are."""

    def listing(self, files):
        with tempfile.TemporaryDirectory() as d:
            for name, body in files.items():
                with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                    fh.write(body)
            keep_snaps, keep_argv = sn.SNAPS, sys.argv
            sn.SNAPS, sys.argv = d, ["snapshot.py", "--list"]
            try:
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    rc = sn.main()
            finally:
                sn.SNAPS, sys.argv = keep_snaps, keep_argv
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_an_unstamped_full_copy_is_flagged(self):
        out = self.listing({"lexicon-2026-08-23-2309.html": PAGE})
        self.assertIn(f"full copy — {sn.STALE}", out)

    def test_a_current_build_is_not_flagged(self):
        out = self.listing({"lexicon-2026-08-29-1200.html": bd.stamp(PAGE)})
        self.assertIn("full copy)", out)
        self.assertNotIn(sn.STALE, out)

    def test_a_stale_shareable_copy_is_flagged_too(self):
        """A share copy built by the leakier version is the worse case: it is
        the one that was made to be handed over."""
        out = self.listing({"lexicon-2026-08-23-2309-shareable.html": PAGE})
        self.assertIn(f"shareable — {sn.STALE}", out)

    def test_a_snapshot_below_the_current_schema_is_flagged(self):
        """Not just the unmarked ones: a bump to REDACTION_SCHEMA has to make
        yesterday's stamped snapshots stale as well."""
        out = self.listing(
            {"lexicon-old.html": bd.stamp(PAGE, bd.REDACTION_SCHEMA - 1),
             "lexicon-new.html": bd.stamp(PAGE)})
        lines = [ln for ln in out.splitlines() if ".html" in ln]
        self.assertEqual(len(lines), 2)
        self.assertIn(sn.STALE, [ln for ln in lines if "old" in ln][0])
        self.assertNotIn(sn.STALE, [ln for ln in lines if "new" in ln][0])

    def test_the_listing_explains_itself_once(self):
        out = self.listing({"a.html": PAGE, "b.html": PAGE,
                            "c.html": bd.stamp(PAGE)})
        self.assertEqual(out.count("rebuild before sharing"), 2, "one per row")
        self.assertIn("2 built under redaction rules older than", out)
        self.assertIn("re-run the pipeline", out)

    def test_no_snapshots_still_works(self):
        self.assertIn("no snapshots yet", self.listing({}))


class TestTakingASnapshotWarnsOnAStalePage(unittest.TestCase):
    """Copying a page built before the marker produces a snapshot that --list
    will flag. Saying so at the moment of copying is cheaper than finding out
    after it has been sent."""

    def take(self, page):
        with tempfile.TemporaryDirectory() as d:
            dash = os.path.join(d, "dashboard.html")
            with open(dash, "w", encoding="utf-8") as fh:
                fh.write(page)
            keep = (sn.DASH, sn.SNAPS, sn.STATS, sys.argv)
            sn.DASH = dash
            sn.SNAPS = os.path.join(d, "snapshots")
            sn.STATS = os.path.join(d, "no-such-stats.json")
            sys.argv = ["snapshot.py"]
            try:
                with contextlib.redirect_stdout(io.StringIO()) as buf:
                    rc = sn.main()
            finally:
                sn.DASH, sn.SNAPS, sn.STATS, sys.argv = keep
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_a_stale_source_page_is_called_out(self):
        self.assertIn(sn.STALE, self.take(PAGE))

    def test_a_current_source_page_is_not(self):
        self.assertNotIn(sn.STALE, self.take(bd.stamp(PAGE)))


class TestTheRealSnapshotsAreOnlyRead(unittest.TestCase):
    """The owner's snapshots/ is data, not a fixture. This suite must be able
    to run against it without changing it."""

    def test_listing_the_real_directory_is_read_only(self):
        snaps = os.path.join(ROOT, "snapshots")
        if not os.path.isdir(snaps):
            self.skipTest("no snapshots/ on this machine")
        before = sorted((f, os.path.getsize(os.path.join(snaps, f)),
                         os.path.getmtime(os.path.join(snaps, f)))
                        for f in os.listdir(snaps))
        keep = sys.argv
        sys.argv = ["snapshot.py", "--list"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                rc = sn.main()
        finally:
            sys.argv = keep
        self.assertEqual(rc, 0)
        after = sorted((f, os.path.getsize(os.path.join(snaps, f)),
                        os.path.getmtime(os.path.join(snaps, f)))
                       for f in os.listdir(snaps))
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
