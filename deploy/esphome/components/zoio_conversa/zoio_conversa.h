#pragma once

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/microphone/microphone_source.h"
#include "esphome/components/ring_buffer/ring_buffer.h"
#include "esphome/components/speaker/speaker.h"

#include <mbedtls/gcm.h>
#include <lwip/sockets.h>

#include <atomic>
#include <memory>
#include <string>
#include <vector>

namespace esphome::zoio_conversa {

/// Conversa com o portão: microfone e alto-falante da placa, ligados ao Zóio
/// por UDP com AES-128-GCM. A chave chega pela API cifrada a cada conversa.
///
/// Pacote: 'Z' | tipo | contador (8 bytes, big-endian) | áudio cifrado | etiqueta (16 bytes).
/// Nonce: direção (1 byte) | 3 zeros | contador. O cabeçalho entra como dado autenticado.
/// Áudio: PCM 16 bits, 16 kHz, mono, little-endian.
class ZoioConversa : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_CONNECTION; }

  void set_microphone_source(microphone::MicrophoneSource *mic) { this->mic_ = mic; }
  void set_speaker(speaker::Speaker *speaker) { this->speaker_ = speaker; }
  void set_port(uint16_t port) { this->port_ = port; }
  void set_max_duration(uint32_t ms) { this->max_duration_ = ms; }

  /// Começa uma conversa com a chave em hexadecimal (32 caracteres). Se já
  /// houver uma, ela é encerrada antes.
  void start(const std::string &key_hex);
  void stop();
  bool is_active() const { return this->active_; }

 protected:
  bool open_socket_();
  void close_socket_();
  void receive_();
  void send_microphone_();
  void play_();
  void send_(uint8_t type, const uint8_t *data, size_t len);

  microphone::MicrophoneSource *mic_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  uint16_t port_{6054};
  uint32_t max_duration_{600000};

  std::unique_ptr<ring_buffer::RingBuffer> mic_buffer_;
  std::vector<uint8_t> speaker_buffer_;  // áudio do Zóio esperando o alto-falante
  mbedtls_gcm_context gcm_{};
  int fd_{-1};
  std::atomic<bool> active_{false};
  bool has_peer_{false};
  bool playing_{false};
  struct sockaddr_in peer_{};
  uint64_t tx_counter_{0};
  uint64_t rx_counter_{0};
  uint32_t started_{0};
  uint32_t last_rx_{0};
  uint32_t last_tx_{0};
  uint32_t last_audio_{0};
  HighFrequencyLoopRequester high_freq_;
};

}  // namespace esphome::zoio_conversa
