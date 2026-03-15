import { describe, expect, it } from "vitest";

import {
  extractSourceExtension,
  isWecomAudioSource,
  resolveWecomVoiceSourceExtension,
  shouldTranscodeWecomVoice,
} from "./voice.js";

describe("wecom-app voice helpers", () => {
  it("detects common audio files as voice candidates", () => {
    expect(isWecomAudioSource("demo.wav")).toBe(true);
    expect(isWecomAudioSource("demo.mp3")).toBe(true);
    expect(isWecomAudioSource("demo.ogg")).toBe(true);
    expect(isWecomAudioSource("demo.m4a")).toBe(true);
    expect(isWecomAudioSource("demo.aac")).toBe(true);
    expect(isWecomAudioSource("demo.flac")).toBe(true);
  });

  it("keeps amr and speex as native WeCom voice formats", () => {
    expect(shouldTranscodeWecomVoice("demo.amr")).toBe(false);
    expect(shouldTranscodeWecomVoice("demo.speex")).toBe(false);
    expect(shouldTranscodeWecomVoice("https://example.com/voice", "audio/amr")).toBe(false);
    expect(shouldTranscodeWecomVoice("https://example.com/voice", "audio/speex")).toBe(false);
  });

  it("marks non-native audio formats for AMR transcode", () => {
    expect(shouldTranscodeWecomVoice("demo.wav")).toBe(true);
    expect(shouldTranscodeWecomVoice("demo.mp3")).toBe(true);
    expect(shouldTranscodeWecomVoice("demo.ogg")).toBe(true);
    expect(shouldTranscodeWecomVoice("https://example.com/stream", "audio/wav")).toBe(true);
  });

  it("extracts source extensions from local paths and URLs with query strings", () => {
    expect(extractSourceExtension("C:/tmp/voice.MP3")).toBe("mp3");
    expect(extractSourceExtension("https://example.com/a/b/test.ogg?download=1")).toBe("ogg");
  });

  it("falls back to mime type when the source name has no extension", () => {
    expect(resolveWecomVoiceSourceExtension("https://example.com/audio", "audio/x-m4a")).toBe(".m4a");
    expect(resolveWecomVoiceSourceExtension("https://example.com/audio", "audio/amr")).toBe(".amr");
  });
});
