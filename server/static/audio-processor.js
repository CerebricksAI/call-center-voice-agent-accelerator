class RingBufferProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(0);
    // True while the agent's audio is actively playing; used to fire a single
    // "drained" notification when playback reaches its natural end.
    this.hadData = false;
    this.port.onmessage = e => {
      if (e.data.pcm) {
        const next = new Float32Array(this.buffer.length + e.data.pcm.length);
        next.set(this.buffer);
        next.set(e.data.pcm, this.buffer.length);
        this.buffer = next;
      } else if (e.data.clear) {
        // Barge-in flush — drop buffered audio WITHOUT reporting a drain
        // (an interruption is not a natural end of speech).
        this.buffer = new Float32Array(0);
        this.hadData = false;
      }
    };
  }

  process(_, outputs) {
    const out = outputs[0][0];
    if (this.buffer.length >= out.length) {
      out.set(this.buffer.subarray(0, out.length));
      this.buffer = this.buffer.subarray(out.length);
      this.hadData = true;
    } else {
      out.fill(0);
      this.buffer = new Float32Array(0);
      if (this.hadData) {
        // Playback just drained to empty — report the true end of speech so the
        // server ends the call only after the client finishes playing.
        this.hadData = false;
        this.port.postMessage({ drained: true });
      }
    }
    return true;
  }
}

registerProcessor('audio-processor', RingBufferProcessor);
