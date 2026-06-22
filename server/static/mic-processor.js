class MicProcessor extends AudioWorkletProcessor {
  process(inputs) {
    const input = inputs[0]?.[0];
    if (input && input.length) {
      this.port.postMessage(input);
    }
    return true;
  }
}

registerProcessor("mic-processor", MicProcessor);
