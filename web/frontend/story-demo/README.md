# A2A Story Demo (Doodle + Better Voice)

This version ports voice style logic from `/local/mnt/workspace/tmp/test.html` and adds animated doodle speaker behavior.

## Paths
- HTML: `/local/mnt/workspace/A2A_CLI/web/frontend/story-demo/index.html`
- Images: `/local/mnt/workspace/A2A_CLI/web/frontend/story-demo/assets/images`
- Recorded audio fallback: `/local/mnt/workspace/A2A_CLI/web/frontend/story-demo/assets/audio`

## Run
```bash
cd /local/mnt/workspace/A2A_CLI/web/frontend/story-demo
python3 -m http.server 8022 --bind 0.0.0.0
```

Open:
- Local: `http://localhost:8022`
- LAN/VPN: `http://10.147.254.52:8022`

## Voice Behavior
- Default mode: `Mode: Browser TTS` (uses en-IN preferred voice selection heuristics).
- Fallback mode: `Mode: Recorded` (plays `s1..s9.mp3`).
- Speaker spotlight and typed dialogue bubble are animated per scene.

## Autoplay Note
- Scene timeline auto-starts on load.
- Some browsers may block first audio playback until one user interaction.
- If that happens, click `Pause` then `Play` once.

## Controls
- `Prev / Next`
- `Pause / Play`
- `Voice On/Off`
- `Mode: Browser TTS / Mode: Recorded`
- Keyboard: `Left/Right` to navigate, `Space` to play/pause.
