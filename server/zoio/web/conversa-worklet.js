// Áudio da conversa com o portão, fora da thread principal (AudioWorklet).
// A placa fala PCM 16 bits, 16 kHz, mono; o celular costuma rodar a 48 kHz.

const RATE = 16000;
const FRAME = 320; // 20 ms a 16 kHz

// Microfone: reduz para 16 kHz (média das amostras de cada intervalo, que
// também corta o agudo que viraria chiado) e manda quadros de 20 ms.
class Microfone extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / RATE;
    this.t = 0;
    this.sum = 0;
    this.count = 0;
    this.frame = new Int16Array(FRAME);
    this.n = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (let i = 0; i < input.length; i++) {
      this.sum += input[i];
      this.count++;
      if (++this.t < this.step) continue;
      this.t -= this.step;
      const v = Math.max(-1, Math.min(1, this.sum / this.count));
      this.sum = 0;
      this.count = 0;
      this.frame[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.n === FRAME) {
        this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
        this.frame = new Int16Array(FRAME);
        this.n = 0;
      }
    }
    return true;
  }
}

// Alto-falante: fila curta para a rede não picotar o som. Junta 100 ms antes
// de tocar e, se atrasar mais de 400 ms, pula o mais velho para não acumular.
const START = RATE / 10;
const MAX = (RATE * 4) / 10;

class AltoFalante extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buf = new Float32Array(RATE * 2);
    this.read = 0; // posição (fracionária) de leitura
    this.write = 0;
    this.playing = false;
    this.muted = false;
    this.step = RATE / sampleRate;
    this.port.onmessage = (e) => {
      if (e.data === 'mute') { this.muted = true; this.read = this.write = 0; this.playing = false; return; }
      if (e.data === 'unmute') { this.muted = false; return; }
      if (!this.muted) this.push(new Int16Array(e.data));
    };
  }

  get size() { return this.write - Math.floor(this.read); }

  push(pcm) {
    if (this.write + pcm.length > this.buf.length) { // volta para o começo do buffer
      const keep = this.buf.subarray(Math.floor(this.read), this.write);
      this.buf.copyWithin(0, Math.floor(this.read), this.write);
      this.read -= Math.floor(this.read);
      this.write = keep.length;
    }
    if (this.size + pcm.length > MAX) this.read = Math.max(this.read, this.write + pcm.length - START);
    for (let i = 0; i < pcm.length && this.write < this.buf.length; i++) this.buf[this.write++] = pcm[i] / 0x8000;
  }

  process(_inputs, outputs) {
    const out = outputs[0][0];
    if (!this.playing && this.size >= START) this.playing = true;
    for (let i = 0; i < out.length; i++) {
      const j = Math.floor(this.read);
      if (!this.playing || j + 1 >= this.write) { out[i] = 0; this.playing = false; continue; }
      const f = this.read - j;
      out[i] = this.buf[j] * (1 - f) + this.buf[j + 1] * f; // interpolação linear até a taxa do aparelho
      this.read += this.step;
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}

registerProcessor('zoio-microfone', Microfone);
registerProcessor('zoio-alto-falante', AltoFalante);
