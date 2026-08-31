from . import antigravity, claude_code, codex, cursor, gemini_cli, grok

ADAPTERS = {
    m.NAME: m for m in (claude_code, codex, grok, gemini_cli, antigravity,
                        cursor)
}
