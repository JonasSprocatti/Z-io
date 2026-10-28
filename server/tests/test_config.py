from zoio import config


def test_env_file_keeps_unquoted_spaces_and_quotes(tmp_path, monkeypatch):
    for key in ("DVR_HOST", "DVR_USER", "DVR_PASS", "ZOIO_CHANNELS", "ZOIO_CHANNEL_NAMES", "ZOIO_HIDE_CHANNELS"):
        monkeypatch.delenv(key, raising=False)
    env = tmp_path / "zoio.env"
    env.write_text(
        "# comentário\n"
        "DVR_HOST=192.168.15.6\n"
        "export DVR_USER=admin  # usuário do DVR\n"
        "DVR_PASS='s3nh@ com espaço#e'\n"
        "ZOIO_CHANNELS=4\n"
        "ZOIO_CHANNEL_NAMES=Câmera 1,Portão da frente, Quintal ,\n"
        "ZOIO_HIDE_CHANNELS=\n",
        encoding="utf-8",
    )
    cfg = config.load(str(env))
    assert (cfg.dvr_host, cfg.dvr_user, cfg.dvr_pass) == ("192.168.15.6", "admin", "s3nh@ com espaço#e")
    assert [c.name for c in cfg.cameras] == ["Câmera 1", "Portão da frente", "Quintal", "Câmera 4"]


def test_unquoted_value_keeps_hash_without_space():
    assert config._env_value("abc#def") == "abc#def"
    assert config._env_value("abc #def") == "abc"
    assert config._env_value('"a b" # c') == "a b"
    assert config._env_value("") == ""
