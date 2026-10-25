from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = Path(os.getenv("SONG_ENV_FILE", str(ROOT / ".env"))).resolve()
load_dotenv(ENV_FILE, override=False)


@dataclass(frozen=True)
class Settings:
    ace_url: str = os.getenv("ACESTEP_API_URL", "http://192.168.40.55:8001").rstrip("/")
    data_dir: Path = Path(os.getenv("SONG_DATA_DIR", str(ROOT / "data"))).resolve()
    model: str = os.getenv("SONG_MODEL", "acestep-v15-xl-sft")
    lm_model: str = os.getenv("SONG_LM_MODEL", "acestep-5Hz-lm-1.7B")
    webhook_url: str = os.getenv("SONG_WEBHOOK_URL", "").strip()
    push_url: str = os.getenv("SONG_NTFY_URL", "").strip()
    comfy_url: str = os.getenv("COMFYUI_API_URL", "http://192.168.40.55:8188").rstrip("/")
    comfy_unet: str = os.getenv("COMFYUI_UNET", "z_image_turbo_bf16.safetensors")
    comfy_clip: str = os.getenv("COMFYUI_CLIP", "qwen_3_4b.safetensors")
    comfy_vae: str = os.getenv("COMFYUI_VAE", "ae.safetensors")
    poll_seconds: int = 120
    ace_maintenance_every: int = int(os.getenv("ACESTEP_MAINTENANCE_EVERY", "10"))
    ace_submit_retries: int = int(os.getenv("ACESTEP_SUBMIT_RETRIES", "6"))
    ace_submit_retry_seconds: int = int(os.getenv("ACESTEP_SUBMIT_RETRY_SECONDS", "5"))


settings = Settings()
