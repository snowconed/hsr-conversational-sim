/* Convert the browser's actual sample rate to mono PCM16 little-endian at 24 kHz. */
class RobotPCMRecorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 24000;
    this.inputSamples = [];
    this.position = 0;
    this.chunk = new Int16Array(2400);
    this.chunkLength = 0;
    this.active = true;
    this.levelSamples = 0;
    this.levelSquared = 0;
    this.port.onmessage = event => {
      if (event.data.type !== "flush" || !this.active) return;
      this.active = false;
      // Repeat the last source sample only to finish the final interpolation interval.
      if (this.inputSamples.length) {
        this.inputSamples.push(this.inputSamples[this.inputSamples.length - 1]);
        this.resample();
      }
      this.sendChunk();
      this.port.postMessage({type:"flushed"});
    };
  }

  sendChunk() {
    if (!this.chunkLength) return;
    // DataView makes wire endianness explicit rather than relying on the platform.
    const buffer = new ArrayBuffer(this.chunkLength * 2);
    const view = new DataView(buffer);
    for (let i = 0; i < this.chunkLength; i++) view.setInt16(i * 2, this.chunk[i], true);
    this.port.postMessage({type:"pcm", buffer}, [buffer]);
    this.chunkLength = 0;
  }

  resample() {
    while (this.position + 1 < this.inputSamples.length) {
      const index = Math.floor(this.position);
      const fraction = this.position - index;
      const value = this.inputSamples[index] * (1 - fraction) + this.inputSamples[index + 1] * fraction;
      const clamped = Math.max(-1, Math.min(1, value));
      this.chunk[this.chunkLength++] = Math.round(clamped * (clamped < 0 ? 32768 : 32767));
      if (this.chunkLength === this.chunk.length) this.sendChunk();
      this.position += this.ratio;
    }
    const consumed = Math.min(Math.floor(this.position), this.inputSamples.length);
    this.inputSamples.splice(0, consumed);
    this.position -= consumed;
  }

  process(inputs, outputs) {
    // Keep the output silent even if connected to the destination for processing.
    outputs[0]?.forEach(channel => channel.fill(0));
    if (!this.active) return true;
    const channels = inputs[0];
    if (!channels?.length) return true;
    const count = channels[0].length;
    for (let i = 0; i < count; i++) {
      let value = 0;
      for (const channel of channels) value += channel[i] || 0;
      value /= channels.length;
      this.inputSamples.push(value);
      this.levelSquared += value * value;
      this.levelSamples++;
    }
    this.resample();
    if (this.levelSamples >= sampleRate / 10) {
      this.port.postMessage({type:"level", rms:Math.sqrt(this.levelSquared / this.levelSamples)});
      this.levelSamples = 0;
      this.levelSquared = 0;
    }
    return true;
  }
}

registerProcessor("robot-pcm-recorder", RobotPCMRecorder);
