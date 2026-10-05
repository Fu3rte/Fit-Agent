from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from app.model_config import load_model_config


def check() -> None:
    temporary = Path(__file__).resolve().parents[1] / "temp"
    temporary.mkdir(exist_ok=True)
    with TemporaryDirectory(dir=temporary) as directory:
        path = Path(directory) / ".env"
        path.write_text(
            'OPENAI_API_KEY="local-check-key"\n'
            'OPENAI_BASE_URL="http://127.0.0.1:8000/v1"\n'
            'OPENAI_MODEL="local-model" # 配置解析检查\n'
            'OPENAI_PROVIDER="local-provider"\n',
            encoding="utf-8",
        )
        config = load_model_config(path)
        assert config.OPENAI_API_KEY == "local-check-key"
        assert config.OPENAI_MODEL == "local-model"
        assert config.OPENAI_PROVIDER == "local-provider"
        assert "local-check-key" not in repr(config)
        assert config.MODEL_API == "openai-completions"
        with config.create_client() as client:
            assert str(client.base_url) == "http://127.0.0.1:8000/v1/"
            assert client.max_retries == 0
            assert client.timeout == 60
        path.write_text(
            'OPENAI_API_KEY="local-check-key"\n'
            'OPENAI_BASE_URL="http://127.0.0.1:8000/v1"\n'
            'OPENAI_MODEL="local-model"\n',
            encoding="utf-8",
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(load_model_config, path).exception()
        assert isinstance(failure, ValidationError)
        assert "OPENAI_PROVIDER" in str(failure)
        assert "local-check-key" not in str(failure)
        path.write_text(
            'OPENAI_API_KEY="local-check-key"\n'
            'OPENAI_BASE_URL="http://127.0.0.1:8000/v1"\n'
            'OPENAI_MODEL="local-model"\n'
            'OPENAI_PROVIDER="local-provider"\n'
            'MODEL_API="missing-protocol"\n',
            encoding="utf-8",
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            failure = executor.submit(load_model_config, path).exception()
        assert isinstance(failure, ValidationError)
        assert "未注册的协议" in str(failure)
    print(".env 配置解析、协议选择与 OpenAI 客户端装配检查通过")


if __name__ == "__main__":
    check()
