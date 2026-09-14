# Text2Post-Agent

Turn any project idea into a TikTok full completed post via script to video pipeline.

## About The Project

Have you ever found yourself in a situation where editing a video 
takes a lot of time and effort? Then this is exactly for you 
this tool lets you write a short prompt about what you want to 
post and it'll do it for you almost automatically.

## How It Works
for the agent to complete its pipeline it goes throughout few steps
the user writes prompt to gemini the agent may ask follow up questions if the prompt too short or more details needed separately the script passes through words/letters filter that signs for ai slop (low quality clichéd ai content) after that gemini writes a script according to the prompt then creates a carousel post format or a video then finally posts it on your account with approval

## Requirements

* ![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
* ![FFmpeg](https://img.shields.io/badge/FFmpeg-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)
* ![Gemini API](https://img.shields.io/badge/Gemini%20API-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
* ![Zernio API](https://img.shields.io/badge/Zernio%20API-000000?style=for-the-badge)

Install dependencies:

```bash
pip install -r requirements.txt
```
## Getting started
### Windows PowerShell (verified end to end)

```bash
cd hackreel
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:GEMINI_API_KEY = "your-key-here"
.\.venv\Scripts\python.exe -m uvicorn service.main:app --port 8000
```
for publishing
```bash
.\.venv\Scripts\python.exe service\publish.py assets\output\<bundle-name> --send
```
Open http://127.0.0.1:8000/

macOS / Linux
```
cd hackreel && python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export GEMINI_API_KEY="your-key-here"
.venv/bin/python -m uvicorn service.main:app --port 8000
```
Open http://127.0.0.1:8000/

for publishing 

```bash
 .venv/bin/python service/publish.py assets/output/<bundle-name> --send 
```

for Tiktok publish there are additional requirements: 

from zernio:
ZERNIO_API_KEY
&
ZERNIO_ACCOUNT_ID

from ntfy:
NTFY_TOPIC
&
NTFY_REPLY_TOPIC


## Built with
* ![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi&logoColor=white)
* ![Pydantic](https://img.shields.io/badge/Pydantic-E92063?style=for-the-badge&logo=pydantic&logoColor=white)
* ![Google Gemini](https://img.shields.io/badge/Google%20Gemini-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
* ![Claude Code](https://img.shields.io/badge/Claude%20Code-D97757?style=for-the-badge&logo=anthropic&logoColor=white)
* ![FFmpeg](https://img.shields.io/badge/FFmpeg-007808?style=for-the-badge&logo=ffmpeg&logoColor=white)
* ![Uvicorn](https://img.shields.io/badge/Uvicorn-499885?style=for-the-badge&logo=uvicorn&logoColor=white)
* ![Pillow](https://img.shields.io/badge/Pillow-000000?style=for-the-badge&logo=python&logoColor=white)

Claude Code was used throughout development while Gemini API generates the script content at runtime.
