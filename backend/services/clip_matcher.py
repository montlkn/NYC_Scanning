import torch
import open_clip
from PIL import Image
import numpy as np
from io import BytesIO

_model = None
_preprocess = None

def get_model():
    global _model, _preprocess
    if _model is None:
        print('Loading CLIP model...')
        _model, _, _preprocess = open_clip.create_model_and_transforms(
            'ViT-B-32', 
            pretrained='openai'
        )
        _model.eval()
        print('CLIP model loaded')
    return _model, _preprocess

async def encode_photo(photo_bytes):
    model, preprocess = get_model()
    
    image = Image.open(BytesIO(photo_bytes))
    image_tensor = preprocess(image).unsqueeze(0)
    
    with torch.no_grad():
        embedding = model.encode_image(image_tensor)
    
    # Normalize
    embedding = embedding / embedding.norm(dim=-1, keepdim=True)
    return embedding.cpu().numpy()[0]
