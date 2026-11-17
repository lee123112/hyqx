from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common.augmentation_api import create_app
import processor
app = create_app(processor, "算子24 跨语言合成", "24_cross_lingual_synthesized.jsonl")
if __name__ == "__main__":
    import uvicorn; uvicorn.run(app, host="0.0.0.0", port=8024)
