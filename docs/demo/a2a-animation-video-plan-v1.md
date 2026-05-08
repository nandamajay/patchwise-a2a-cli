# A2A Animated Demo - Production Plan v1

## Goal
Create a story-style animated demo with Indian English voiceover, humor beats, and clear technical explanation of Agent2Agent (A2A).

## Output Targets
- Primary: interactive animated HTML demo (already scaffolded)
- Optional: narrated MP4 export for async sharing

## Current Base
- Storyboard: `/local/mnt/workspace/A2A_CLI/docs/demo/a2a-storyboard-v1.md`
- Scene cues: `/local/mnt/workspace/A2A_CLI/web/frontend/story-demo/cue-sheet.json`
- Player: `/local/mnt/workspace/A2A_CLI/web/frontend/story-demo/index.html`

## Content Structure (Narrative Arc)
1. Virtual Ajay intro + salary joke
2. QGenie babysitting pain
3. Why A2A was born
4. A2A architecture (builder/reviewer/validation gate)
5. Embedded jobs + chai/samosa comic break
6. Why A2A improves reliability
7. Capability walkthrough
8. Beyond patch review
9. Closing line

## Voice Strategy
- Character A (Virtual Ajay): hero tone, lower pace, punchy emphasis
- Character B (Ajay narration): warm explanatory tone
- Comic lines: slightly faster cadence and smile in delivery

### Recommended Voice Specs
- Sample rate: 48 kHz
- Bit depth: 24-bit (or 16-bit if tool limited)
- Noise floor: below -50 dB
- Loudness target: around -16 LUFS (stereo web delivery)

## First-Cut Plan (Now)
1. Validate script timing scene-by-scene using browser TTS.
2. Check subtitle readability and humor placement.
3. Ensure scene transitions are smooth on laptop + mobile.
4. Freeze cue timing once flow feels natural.

## Second-Cut Plan (Voice Upgrade)
1. Record per-scene narration (`s1` to `s9`) as separate files.
2. Save as `assets/audio/s1.mp3` ... `assets/audio/s9.mp3`.
3. In player, switch `Mode: Scene Audio` and test complete run.
4. Adjust pauses and line emphasis where needed.

## Final-Cut Plan (Presentation Ready)
1. Polish wording of 1-2 technical lines for clarity.
2. Add/select meme images where visual punch is needed.
3. Perform full end-to-end rehearsal without manual interruptions.
4. Export screen recording to MP4 if needed.

## Suggested Timeline
- Script/timing cleanup: 45-60 min
- Voice recording pass: 60-90 min
- Audio cleanup and alignment: 45 min
- Final rehearsal/export: 30 min

## Risk Checklist
- `cue-sheet.json` not loading if opened as file path:
  - Mitigation: run using local HTTP server.
- Browser autoplay restrictions for audio:
  - Mitigation: click `Play` once manually before full run.
- Voice mismatch across scenes:
  - Mitigation: record all scenes in one session with fixed mic distance.

## Run Commands
```bash
cd /local/mnt/workspace/A2A_CLI/web/frontend/story-demo
python3 -m http.server 8022
```
Open: `http://localhost:8022`

## Iteration Loop
1. Play full demo.
2. Note weak scenes/timecodes.
3. Edit `cue-sheet.json` or replace scene audio.
4. Replay and compare.
5. Freeze cut when transitions + humor + clarity are all stable.
