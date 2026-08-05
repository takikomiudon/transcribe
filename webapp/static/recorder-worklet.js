class RecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const processorOptions = options.processorOptions || {};
    this.targetSampleRate = processorOptions.targetSampleRate || 16000;
    this.sourceSampleRate = processorOptions.sourceSampleRate || sampleRate;
    this.ratio = this.sourceSampleRate / this.targetSampleRate;
    this.sourceBuffer = [];
    this.readCursor = 0;
    this.outputBuffer = [];
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || input.length === 0) return true;

    if (this.sourceSampleRate === this.targetSampleRate) {
      for (const sample of input) this.outputBuffer.push(sample);
    } else {
      for (const sample of input) this.sourceBuffer.push(sample);
      while (this.readCursor + 1 < this.sourceBuffer.length) {
        const index = Math.floor(this.readCursor);
        const fraction = this.readCursor - index;
        const first = this.sourceBuffer[index];
        const second = this.sourceBuffer[index + 1];
        this.outputBuffer.push(first + (second - first) * fraction);
        this.readCursor += this.ratio;
      }

      const consumed = Math.min(
        Math.floor(this.readCursor),
        this.sourceBuffer.length - 1,
      );
      if (consumed > 0) {
        this.sourceBuffer.splice(0, consumed);
        this.readCursor -= consumed;
      }
    }

    while (this.outputBuffer.length >= 1600) {
      const samples = this.outputBuffer.splice(0, 1600);
      const pcm = new Int16Array(samples.length);
      let sumSquares = 0;
      for (let index = 0; index < samples.length; index += 1) {
        const sample = Math.max(-1, Math.min(1, samples[index]));
        sumSquares += sample * sample;
        pcm[index] = Math.round(sample * 32767);
      }
      const level = Math.sqrt(sumSquares / samples.length);
      const buffer = pcm.buffer;
      this.port.postMessage(buffer, [buffer]);
      this.port.postMessage({level});
    }
    return true;
  }
}

registerProcessor("recorder-processor", RecorderProcessor);
