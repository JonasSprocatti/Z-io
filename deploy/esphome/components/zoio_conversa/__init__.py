"""Conversa do porteiro: áudio cifrado entre a placa e o Zóio, por UDP.

O Zóio chama a ação `conversa_iniciar` da API (cifrada) com uma chave nova de
16 bytes, só daquela conversa. A placa abre a porta UDP, liga o microfone e o
alto-falante e troca com o Zóio pacotes cifrados com AES-128-GCM. Sem sinal do
Zóio por alguns segundos, ou passado o tempo máximo, ela encerra sozinha.
"""

import esphome.codegen as cg
from esphome.components import microphone, speaker
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_MICROPHONE, CONF_PORT, CONF_SPEAKER

CODEOWNERS = []
DEPENDENCIES = ["microphone", "speaker", "network"]
AUTO_LOAD = ["audio", "ring_buffer"]

CONF_MAX_DURATION = "tempo_maximo"
SAMPLE_RATE = 16000

zoio_conversa_ns = cg.esphome_ns.namespace("zoio_conversa")
ZoioConversa = zoio_conversa_ns.class_("ZoioConversa", cg.Component)

CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(ZoioConversa),
            cv.Optional(CONF_MICROPHONE, default={}): microphone.microphone_source_schema(
                min_bits_per_sample=16, max_bits_per_sample=16
            ),
            cv.Required(CONF_SPEAKER): cv.use_id(speaker.Speaker),
            cv.Optional(CONF_PORT, default=6054): cv.port,
            cv.Optional(CONF_MAX_DURATION, default="10min"): cv.positive_time_period_milliseconds,
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on_esp32,
    cv.only_with_framework("esp-idf"),
)

FINAL_VALIDATE_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_MICROPHONE): microphone.final_validate_microphone_source_schema(
            "zoio_conversa", sample_rate=SAMPLE_RATE
        ),
    },
    extra=cv.ALLOW_EXTRA,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    mic_source = await microphone.microphone_source_to_code(config[CONF_MICROPHONE])
    cg.add(var.set_microphone_source(mic_source))
    cg.add(var.set_speaker(await cg.get_variable(config[CONF_SPEAKER])))
    cg.add(var.set_port(config[CONF_PORT]))
    cg.add(var.set_max_duration(config[CONF_MAX_DURATION]))
