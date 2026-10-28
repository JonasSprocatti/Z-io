#include "zoio_conversa.h"

#include "esphome/components/audio/audio.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <mbedtls/platform_util.h>

#include <cerrno>
#include <cstring>

namespace esphome::zoio_conversa {

static const char *const TAG = "zoio_conversa";

static constexpr uint8_t MAGIC = 'Z';
static constexpr uint8_t TYPE_AUDIO = 1;
static constexpr uint8_t TYPE_ALIVE = 2;
static constexpr uint8_t TYPE_END = 3;
static constexpr uint8_t DIR_TO_BOARD = 1;
static constexpr uint8_t DIR_FROM_BOARD = 2;
static constexpr size_t HEADER = 10;
static constexpr size_t TAG_LEN = 16;
static constexpr size_t KEY_LEN = 16;
static constexpr size_t FRAME = 640;             // 20 ms de áudio
static constexpr size_t MAX_PAYLOAD = 2048;      // o que o Zóio pode mandar num pacote
static constexpr size_t MIC_BUFFER = 16000;      // 0,5 s
static constexpr size_t SPEAKER_MAX = 16000;     // acima disso, descarta o mais antigo (atraso)
static constexpr size_t SPEAKER_START = 3200;    // junta 100 ms antes de começar a tocar
static constexpr uint32_t ALIVE_EVERY_MS = 1000;
static constexpr uint32_t SILENT_AFTER_MS = 6000;
static constexpr uint32_t HALF_DUPLEX_MS = 400;  // sem microfone enquanto o alto-falante toca

static void make_nonce(uint8_t *nonce, uint8_t direction, uint64_t counter) {
  nonce[0] = direction;
  nonce[1] = nonce[2] = nonce[3] = 0;
  for (int i = 11; i >= 4; i--) {
    nonce[i] = counter & 0xFF;
    counter >>= 8;
  }
}

void ZoioConversa::setup() {
  this->mic_buffer_ = ring_buffer::RingBuffer::create(MIC_BUFFER);
  if (this->mic_buffer_ == nullptr) {
    ESP_LOGE(TAG, "Sem memória para o microfone");
    this->mark_failed();
    return;
  }
  // O microfone chama isto na tarefa dele; o buffer em anel é seguro entre tarefas.
  this->mic_->add_data_callback([this](const std::vector<uint8_t> &data) {
    if (this->active_.load(std::memory_order_relaxed))
      this->mic_buffer_->write(data.data(), data.size());
  });
  this->speaker_->set_audio_stream_info(audio::AudioStreamInfo(16, 1, 16000));
}

void ZoioConversa::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Conversa do porteiro:\n"
                "  Porta UDP: %u\n"
                "  Tempo máximo: %u s",
                this->port_, (unsigned) (this->max_duration_ / 1000));
}

void ZoioConversa::start(const std::string &key_hex) {
  uint8_t key[KEY_LEN];
  if (key_hex.size() != 2 * KEY_LEN || !parse_hex(key_hex, key, KEY_LEN)) {
    ESP_LOGW(TAG, "Chave da conversa inválida");
    return;
  }
  if (this->active_)
    this->stop();

  mbedtls_gcm_init(&this->gcm_);
  int err = mbedtls_gcm_setkey(&this->gcm_, MBEDTLS_CIPHER_ID_AES, key, KEY_LEN * 8);
  mbedtls_platform_zeroize(key, sizeof(key));
  if (err != 0 || !this->open_socket_()) {
    ESP_LOGE(TAG, "Não consegui começar a conversa (%d)", err);
    mbedtls_gcm_free(&this->gcm_);
    return;
  }

  this->mic_buffer_->reset();
  this->speaker_buffer_.clear();
  this->tx_counter_ = this->rx_counter_ = 0;
  this->has_peer_ = this->playing_ = false;
  this->started_ = this->last_rx_ = millis();
  this->last_tx_ = this->last_audio_ = 0;
  this->active_ = true;
  this->mic_->start();
  this->high_freq_.start();
  ESP_LOGI(TAG, "Conversa iniciada");
}

void ZoioConversa::stop() {
  if (!this->active_)
    return;
  if (this->has_peer_)
    this->send_(TYPE_END, nullptr, 0);
  this->active_ = false;
  this->mic_->stop();
  this->speaker_->stop();
  this->close_socket_();
  mbedtls_gcm_free(&this->gcm_);
  this->speaker_buffer_.clear();
  this->speaker_buffer_.shrink_to_fit();
  this->high_freq_.stop();
  ESP_LOGI(TAG, "Conversa encerrada");
}

bool ZoioConversa::open_socket_() {
  this->fd_ = lwip_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (this->fd_ < 0) {
    ESP_LOGE(TAG, "Não consegui criar o socket (errno %d)", errno);
    return false;
  }
  int flags = lwip_fcntl(this->fd_, F_GETFL, 0);
  lwip_fcntl(this->fd_, F_SETFL, flags | O_NONBLOCK);
  struct sockaddr_in addr {};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(this->port_);
  addr.sin_addr.s_addr = htonl(INADDR_ANY);
  if (lwip_bind(this->fd_, (struct sockaddr *) &addr, sizeof(addr)) != 0) {
    ESP_LOGE(TAG, "Não consegui abrir a porta %u (errno %d)", this->port_, errno);
    this->close_socket_();
    return false;
  }
  return true;
}

void ZoioConversa::close_socket_() {
  if (this->fd_ >= 0) {
    lwip_close(this->fd_);
    this->fd_ = -1;
  }
}

void ZoioConversa::loop() {
  if (!this->active_)
    return;
  this->receive_();
  if (!this->active_)
    return;  // o Zóio encerrou
  this->send_microphone_();
  this->play_();

  uint32_t now = millis();
  if (this->has_peer_ && now - this->last_tx_ >= ALIVE_EVERY_MS)
    this->send_(TYPE_ALIVE, nullptr, 0);
  if (now - this->last_rx_ > SILENT_AFTER_MS) {
    ESP_LOGW(TAG, "O Zóio parou de responder");
    this->stop();
  } else if (now - this->started_ > this->max_duration_) {
    ESP_LOGI(TAG, "Tempo máximo da conversa");
    this->stop();
  }
}

void ZoioConversa::receive_() {
  static uint8_t packet[HEADER + MAX_PAYLOAD + TAG_LEN];
  static uint8_t plain[MAX_PAYLOAD];
  for (int i = 0; i < 16; i++) {  // no máximo 16 pacotes por volta do loop
    struct sockaddr_in from {};
    socklen_t from_len = sizeof(from);
    ssize_t len = lwip_recvfrom(this->fd_, packet, sizeof(packet), 0, (struct sockaddr *) &from, &from_len);
    if (len <= 0)
      return;
    if ((size_t) len < HEADER + TAG_LEN || packet[0] != MAGIC)
      continue;
    uint64_t counter = 0;
    for (size_t b = 2; b < HEADER; b++)
      counter = (counter << 8) | packet[b];
    if (counter <= this->rx_counter_)
      continue;  // repetido ou fora de ordem
    size_t body = (size_t) len - HEADER - TAG_LEN;
    uint8_t nonce[12];
    make_nonce(nonce, DIR_TO_BOARD, counter);
    if (mbedtls_gcm_auth_decrypt(&this->gcm_, body, nonce, sizeof(nonce), packet, HEADER, packet + HEADER + body,
                                 TAG_LEN, packet + HEADER, plain) != 0)
      continue;  // não veio do Zóio desta conversa

    this->rx_counter_ = counter;
    this->last_rx_ = millis();
    this->peer_ = from;
    this->has_peer_ = true;
    uint8_t type = packet[1];
    if (type == TYPE_END) {
      ESP_LOGI(TAG, "O Zóio encerrou a conversa");
      this->stop();
      return;
    }
    if (type == TYPE_AUDIO && body > 0 && body % 2 == 0) {
      size_t total = this->speaker_buffer_.size() + body;
      if (total > SPEAKER_MAX) {  // atrasou demais: descarta o mais antigo
        size_t drop = std::min(this->speaker_buffer_.size(), (total - SPEAKER_MAX + 1) & ~(size_t) 1);
        this->speaker_buffer_.erase(this->speaker_buffer_.begin(), this->speaker_buffer_.begin() + drop);
      }
      this->speaker_buffer_.insert(this->speaker_buffer_.end(), plain, plain + body);
      this->last_audio_ = millis();
    }
  }
}

void ZoioConversa::send_microphone_() {
  uint8_t frame[FRAME];
  while (this->mic_buffer_->available() >= FRAME) {
    if (this->mic_buffer_->read(frame, FRAME, 0) != FRAME)
      return;
    // Sem cancelamento de eco: enquanto o alto-falante toca, o microfone fica de fora.
    if (this->has_peer_ && millis() - this->last_audio_ > HALF_DUPLEX_MS)
      this->send_(TYPE_AUDIO, frame, FRAME);
  }
}

void ZoioConversa::play_() {
  if (this->speaker_buffer_.empty()) {
    if (this->playing_ && millis() - this->last_audio_ > 500)
      this->playing_ = false;  // a próxima fala junta de novo antes de tocar
    return;
  }
  if (!this->playing_) {
    if (this->speaker_buffer_.size() < SPEAKER_START && millis() - this->last_audio_ < 100)
      return;
    this->playing_ = true;
  }
  size_t written = this->speaker_->play(this->speaker_buffer_.data(), this->speaker_buffer_.size());
  if (written > 0)
    this->speaker_buffer_.erase(this->speaker_buffer_.begin(), this->speaker_buffer_.begin() + written);
}

void ZoioConversa::send_(uint8_t type, const uint8_t *data, size_t len) {
  static const uint8_t EMPTY[1] = {0};
  uint8_t packet[HEADER + FRAME + TAG_LEN];
  if (len > FRAME || this->fd_ < 0)
    return;
  uint64_t counter = ++this->tx_counter_;
  packet[0] = MAGIC;
  packet[1] = type;
  for (size_t b = HEADER - 1, c = 0; c < 8; b--, c++)
    packet[b] = (counter >> (8 * c)) & 0xFF;
  uint8_t nonce[12];
  make_nonce(nonce, DIR_FROM_BOARD, counter);
  if (mbedtls_gcm_crypt_and_tag(&this->gcm_, MBEDTLS_GCM_ENCRYPT, len, nonce, sizeof(nonce), packet, HEADER,
                                len ? data : EMPTY, packet + HEADER, TAG_LEN, packet + HEADER + len) != 0)
    return;
  lwip_sendto(this->fd_, packet, HEADER + len + TAG_LEN, 0, (struct sockaddr *) &this->peer_, sizeof(this->peer_));
  this->last_tx_ = millis();
}

}  // namespace esphome::zoio_conversa
