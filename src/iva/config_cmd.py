"""`iva config` — view/set the LLM, STT (ASR) and TTS endpoints Iva uses.

These live in the Hermes config (`~/.hermes/config.yaml`); this command writes the
relevant sections so you don't have to hand-edit YAML.

  iva config show
  iva config set llm.url=http://host:11434/v1 llm.model=gemma4:12b-mlx
  iva config set stt.url=http://host:8000/v1  stt.model=Qwen3-ASR
  iva config set tts.url=http://host:8000/v1  tts.model=kokoro tts.voice=af_heart

Keys: llm.{url,model,key,provider}  stt.{url,model,key}  tts.{url,model,voice,key}
"""
import os
import sys

CONFIG_PATH = os.path.expanduser(os.environ.get("HERMES_CONFIG", "~/.hermes/config.yaml"))

# friendly key -> path in config.yaml
KEYMAP = {
    "llm.url":      ("model", "base_url"),
    "llm.model":    ("model", "default"),
    "llm.key":      ("model", "api_key"),
    "llm.provider": ("model", "provider"),
    "stt.url":      ("stt", "openai", "base_url"),
    "stt.model":    ("stt", "openai", "model"),
    "stt.key":      ("stt", "openai", "api_key"),
    "tts.url":      ("tts", "openai", "base_url"),
    "tts.model":    ("tts", "openai", "model"),
    "tts.voice":    ("tts", "openai", "voice"),
    "tts.key":      ("tts", "openai", "api_key"),
}


def _yaml():
    try:
        import yaml
        return yaml
    except Exception:
        sys.stderr.write("iva config needs PyYAML — pip install pyyaml\n")
        raise SystemExit(1)


def _load():
    yaml = _yaml()
    if os.path.isfile(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def _save(cfg):
    yaml = _yaml()
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)


def _get(cfg, path):
    cur = cfg
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _set(cfg, path, value):
    cur = cfg
    for p in path[:-1]:
        nxt = cur.get(p)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[p] = nxt
        cur = nxt
    cur[path[-1]] = value


def show(cfg=None):
    cfg = cfg if cfg is not None else _load()
    print(f"config: {CONFIG_PATH}\n")
    groups = (
        ("LLM", ("llm.model", "llm.url")),
        ("STT / ASR", ("stt.model", "stt.url")),
        ("TTS", ("tts.model", "tts.voice", "tts.url")),
    )
    for label, keys in groups:
        print(label)
        for k in keys:
            print(f"  {k:11s} = {_get(cfg, KEYMAP[k])}")
    return 0


def config(rest):
    if not rest or rest[0] == "show":
        return show()
    if rest[0] == "set":
        pairs = rest[1:]
        if not pairs:
            sys.stderr.write("usage: iva config set llm.url=... llm.model=... stt.url=... tts.url=...\n")
            return 2
        cfg = _load()
        touched_stt = touched_tts = False
        for pair in pairs:
            if "=" not in pair:
                sys.stderr.write(f"skip {pair!r} (need key=value)\n")
                continue
            k, v = pair.split("=", 1)
            k = k.strip()
            if k not in KEYMAP:
                sys.stderr.write(f"unknown key {k!r}; known: {', '.join(KEYMAP)}\n")
                return 2
            _set(cfg, KEYMAP[k], v)
            touched_stt |= k.startswith("stt.")
            touched_tts |= k.startswith("tts.")
        # ensure Hermes uses the openai-compatible provider for what we set
        if touched_stt:
            _set(cfg, ("stt", "provider"), "openai")
            _set(cfg, ("stt", "enabled"), True)
        if touched_tts:
            _set(cfg, ("tts", "provider"), "openai")
        _save(cfg)
        print(f"updated {CONFIG_PATH}\n")
        return show(cfg)
    sys.stderr.write(f"iva config: unknown subcommand {rest[0]!r} (use: show | set)\n")
    return 2


if __name__ == "__main__":
    sys.exit(config(sys.argv[1:]))
