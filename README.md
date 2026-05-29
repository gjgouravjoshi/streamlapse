# StreamLapse

StreamLapse is a small Flask app for clipping YouTube/video URLs with yt-dlp and ffmpeg.

## Run locally

```bash
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:8080`.

## Deploy on Render

1. Push this folder to a GitHub repository.
2. In Render, create a new Blueprint from the repository.
3. Render will use `render.yaml` and the Dockerfile, including ffmpeg.

Generated clips are stored in `/tmp` on Render and served through the browser download button. Render free instances have temporary storage, so download files soon after creating them.
