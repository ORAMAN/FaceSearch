# -*- coding: utf-8 -*-
"""
Многопоточный HTTP-интерфейс FaceSearch.

Запуск из каталога проекта:
    .venv\Scripts\python.exe server_http.py
    .venv\Scripts\python.exe server_http.py --host 0.0.0.0 --port 8765

Активный профиль, backend, процент сходства по умолчанию и top-k читаются из
database_profiles.json / settings.xml так же, как в GUI. Интерфейс не изменяет
settings.xml и не выполняет миграцию схемы БД.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import face_search_gui as face_search
from facesearch.settings import _load_gui_settings
from facesearch.database_profiles import _load_db_profiles, _default_profile

APP_DIR = Path(__file__).resolve().parent
MAX_BODY_BYTES = 24 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
QUERY_TTL_SECONDS = 30 * 60
MAX_QUERY_SESSIONS = 100

QUERY_SESSIONS: dict[str, dict] = {}
QUERY_SESSIONS_LOCK = threading.RLock()
API_SEMAPHORE = threading.BoundedSemaphore(4)

PROFILE: dict = {}
PROFILE_NAME = ""
BACKEND = face_search.BACKEND_GPU
FACE_APP = None
DEFAULT_TOP_K = 24
DEFAULT_MIN_SIMILARITY = 43.0


def _select_profile() -> tuple[dict, dict]:
    """Выбирает профиль так же, как GUI: сохранённый выбор имеет приоритет."""
    gui_settings = _load_gui_settings()
    profiles, active_name = _load_db_profiles()
    selected_name = str(gui_settings.get("selected_db_profile") or "").strip()
    if selected_name and any(str(p.get("name")) == selected_name for p in profiles):
        active_name = selected_name
    profile = next(
        (dict(p) for p in profiles if str(p.get("name")) == str(active_name)),
        None,
    )
    if profile is None:
        profile = _default_profile()
    return profile, gui_settings


def _open_connection(*, activate_storage: bool = False):
    """Создаёт независимое соединение для текущего HTTP-потока."""
    return face_search.create_database_for_profile(
        PROFILE,
        maintenance=False,
        create_if_missing=False,
        activate_storage=activate_storage,
        initialize=False,
        check_crop_migration=False,
    )


def _initialize_runtime() -> None:
    global PROFILE, PROFILE_NAME, BACKEND, FACE_APP
    global DEFAULT_TOP_K, DEFAULT_MIN_SIMILARITY

    PROFILE, gui_settings = _select_profile()
    PROFILE_NAME = str(PROFILE.get("name") or "БД FaceSearch")

    selected_backend = str(gui_settings.get("backend") or "").strip().lower()
    BACKEND = (
        selected_backend
        if selected_backend in (face_search.BACKEND_GPU, face_search.BACKEND_CPU)
        else face_search.BACKEND_GPU
    )
    DEFAULT_TOP_K = max(1, min(500, int(gui_settings.get("top_k") or 24)))
    DEFAULT_MIN_SIMILARITY = max(
        0.0, min(100.0, float(gui_settings.get("min_similarity", 43.0)))
    )

    # Не позволяем sqlite3 незаметно создать пустой файл вместо потерянной БД.
    if str(PROFILE.get("type") or "sqlite").lower() == "sqlite":
        db_path = Path(str(PROFILE.get("path") or face_search.APP_DIR / "faces.db")).expanduser()
        if not db_path.is_absolute():
            db_path = (face_search.APP_DIR / db_path).resolve()
        if not db_path.is_file():
            raise FileNotFoundError(
                f"Файл активной SQLite-базы не найден: {db_path}. "
                "Откройте GUI и проверьте выбранный профиль."
            )

    # Активируем пути профиля в существующем движке FaceSearch, не меняя схему.
    conn = _open_connection(activate_storage=True)
    try:
        # Таблица faces нужна существующей логике поиска. Запрос выполняется
        # один раз при старте и одновременно проверяет выбранную БД.
        conn.execute("SELECT id FROM faces WHERE 1 = 0").fetchall()
    finally:
        conn.close()

    print(f"[HTTP] Профиль БД: {PROFILE_NAME}")
    print(f"[HTTP] СУБД: {PROFILE.get('type', 'неизвестно')}")
    print(f"[HTTP] Backend: {BACKEND.upper()}")
    print(f"[HTTP] FAISS: {face_search.INDEX_FILE}")

    FACE_APP = face_search.create_face_app(BACKEND)

    index_path = Path(face_search.INDEX_FILE)
    if not index_path.is_file():
        print("[HTTP] ПРЕДУПРЕЖДЕНИЕ: FAISS-индекс не найден; поиск будет сообщать об ошибке.")
    elif BACKEND == face_search.BACKEND_GPU:
        # Пытаемся заранее разместить индекс в VRAM. Если CUDA недоступна или
        # памяти недостаточно, штатный search_embedding использует CPU FAISS.
        try:
            loaded = face_search._load_gpu_search_index_from_faiss(
                index_path,
                progress_callback=lambda message: print("[HTTP][FAISS] " + str(message)),
            )
            if not loaded:
                print("[HTTP] GPU-индекс не активирован; будет использован CPU FAISS.")
        except Exception as exc:
            print(f"[HTTP] Не удалось загрузить FAISS в GPU; fallback на CPU: {exc}")


def _decode_image(data_url: str):
    """Декодирует data URL из браузера с ограничением размера и числа пикселей."""
    if not isinstance(data_url, str) or not data_url:
        raise ValueError("Сначала выберите изображение.")
    if "," in data_url and data_url[:100].lower().startswith("data:"):
        header, encoded = data_url.split(",", 1)
        if ";base64" not in header.lower():
            raise ValueError("Изображение должно быть передано в формате base64.")
    else:
        encoded = data_url

    # Проверяем длину до декодирования, чтобы не выделять неограниченную память.
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 8:
        raise ValueError("Изображение слишком большое. Максимальный размер — 16 МБ.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Не удалось прочитать изображение из браузера.") from exc

    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение пустое или превышает лимит 16 МБ.")

    face_search._ensure_runtime_dependencies(need_numpy=True, need_cv2=True)
    np = face_search.np
    cv2 = face_search.cv2
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Формат изображения не поддерживается или файл повреждён.")
    height, width = image.shape[:2]
    if height <= 0 or width <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("Разрешение изображения слишком велико.")
    return image


def _prune_sessions() -> None:
    now = time.monotonic()
    with QUERY_SESSIONS_LOCK:
        expired = [
            token for token, value in QUERY_SESSIONS.items()
            if now - value["created"] > QUERY_TTL_SECONDS
        ]
        for token in expired:
            QUERY_SESSIONS.pop(token, None)
        while len(QUERY_SESSIONS) > MAX_QUERY_SESSIONS:
            oldest = min(
                QUERY_SESSIONS,
                key=lambda token: QUERY_SESSIONS[token]["created"],
            )
            QUERY_SESSIONS.pop(oldest, None)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>FaceSearch — поиск лиц</title>
<link rel="icon" href="https://raw.githubusercontent.com/ORAMAN/FaceSearch/refs/heads/main/icons/SearchFace.ico" type="image/x-icon">
<style>
:root{
  color-scheme:dark;
  --bg:#151515;--panel:#242424;--line:#383838;--text:#eeeeee;--muted:#aaaaaa;
  --accent:#167ac6;--field:#1c1c1c;--input-line:#515b67;--drop:#1b1b1b;
  --drop-line:#626b76;--image-bg:#111111;--link:#79bfff;--score:#80bdff;
  --secondary:#333333;--secondary-hover:#444444;--info-bg:#1e1e1e;--info-line:#353535;
  --button-text:#eeeeee;--button-line:#484848;--shadow:0 4px 15px rgba(0,0,0,.22)
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#f4f5f7;--panel:#ffffff;--line:#c8cfd8;--text:#202124;--muted:#62666b;
  --accent:#1479c9;--field:#ffffff;--input-line:#b7c0cb;--drop:#ffffff;
  --drop-line:#aab4c0;--image-bg:#e9eaec;--link:#075eb5;--score:#075eb5;
  --secondary:#e5e7eb;--secondary-hover:#d1d5db;--info-bg:#e9edf2;--info-line:#c4ccd5;
  --button-text:#202124;--button-line:#b7c0cb;--shadow:0 4px 15px rgba(30,40,55,.10)
}
*{box-sizing:border-box}
html{min-height:100%;background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,"Segoe UI",Arial,sans-serif;transition:background-color .18s,color .18s}
main{max-width:1600px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:28px;line-height:1.2;margin:0 0 8px}
.subtitle{color:var(--muted);margin:0 0 24px}
.toolbar{display:flex;gap:14px;align-items:end;flex-wrap:wrap;padding:18px;background:var(--panel);border:1px solid var(--line);border-radius:14px;margin-bottom:18px;box-shadow:var(--shadow)}
.field{display:flex;flex-direction:column;gap:5px;min-width:150px}
.field label,.toggle-label{color:var(--muted);font-size:13px}
input[type=number]{width:145px;max-width:100%;background:var(--field);color:var(--text);border:1px solid var(--input-line);border-radius:8px;padding:10px;font:inherit}
input:focus-visible,button:focus-visible,a:focus-visible,canvas:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button{border:0;border-radius:9px;padding:11px 17px;background:var(--accent);color:#fff;font:600 14px system-ui;cursor:pointer;min-height:42px;transition:filter .12s}
button:hover:not(:disabled){filter:brightness(1.08)}
button:disabled{opacity:.42;cursor:not-allowed}
.secondary{background:var(--secondary);color:var(--button-text);border:1px solid var(--button-line)}
.hint{color:var(--muted);font-size:13px}
.toggle-field{display:flex;flex-direction:column;justify-content:flex-end;gap:7px;min-height:42px}
.toggle-label{display:flex;align-items:center;gap:8px;min-height:42px;color:var(--text);font-size:14px;cursor:pointer}
input[type=checkbox]{width:18px;height:18px;margin:0;accent-color:var(--accent);cursor:pointer;flex:0 0 auto}
.dropzone{border:1px dashed var(--drop-line);background:var(--drop);border-radius:14px;min-height:210px;display:flex;align-items:center;justify-content:center;padding:18px;text-align:center;position:relative}
.dropzone.drag{border-color:var(--accent);background:var(--panel)}
.dropzone-content{max-width:650px;width:100%}
.dropzone input{display:block;max-width:100%;margin:13px auto}
#preview{display:none;max-width:100%;max-height:440px;object-fit:contain}
#preview-canvas{display:none;max-width:100%;max-height:440px;width:auto;height:auto;object-fit:contain;cursor:crosshair}
.status{min-height:28px;color:var(--muted);padding:10px 2px;overflow-wrap:anywhere}
.status.error{color:#c83e3e}
#search-button{margin:6px 0 18px}
.section-title{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:23px 0 14px}
.section-title h2{font-size:20px;margin:0}
.section-title span{color:var(--muted);font-size:13px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,265px),1fr));gap:18px}
.card{position:relative;min-width:0;overflow:hidden;border:1px solid var(--line);background:var(--panel);border-radius:12px;padding-bottom:14px;box-shadow:var(--shadow);transition:transform .14s,box-shadow .14s}
.card:hover{transform:translateY(-2px)}
.card-image{height:270px;padding:4px;position:relative;background:var(--image-bg);display:flex;align-items:center;justify-content:center;overflow:hidden}
.card-image img{display:block;width:100%;height:100%;object-fit:contain;border-radius:3px}
.no-image[hidden]{display:none}.no-image{display:flex;align-items:center;justify-content:center;text-align:center;padding:18px;color:var(--muted);font-size:13px}
.rank,.score{position:absolute;top:10px;z-index:2;padding:5px 9px;color:#fff;background:rgba(0,0,0,.82);border-radius:6px;text-shadow:0 1px 2px rgba(0,0,0,.35);font-size:13px}
.rank{left:10px}
.score{right:10px;background:rgba(0,112,205,.92);font-weight:700}
.card-body{padding:0 15px}
.card-title{font-weight:700;line-height:1.35;overflow-wrap:anywhere;padding:10px 0;color:var(--text);border-bottom:1px solid var(--line)}
.card-meta{font-size:12px;color:var(--muted);overflow-wrap:anywhere;margin-top:7px}
.card-meta a,.source-site a{color:var(--link);text-decoration:none}
.card-meta a:hover,.source-site a:hover{text-decoration:underline}
.source-site{display:flex;align-items:center;gap:7px;margin-top:10px;font-size:12px;min-height:22px;min-width:0}
.source-site a{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.favicon{width:16px;height:16px;object-fit:contain;flex:0 0 16px}
.card-info{margin:10px 0 12px;padding:10px;background:var(--info-bg);border:1px solid var(--info-line);border-radius:8px;line-height:1.55;font-size:13px;overflow-wrap:anywhere}
.card-info .info-name{color:var(--link);font-size:15px;font-weight:700;margin-bottom:5px}
.card-info-row + .card-info-row{margin-top:3px}
.card-info-row b{font-weight:650}
.card-buttons{display:flex;gap:7px;margin-top:12px}
.card-button{display:flex;flex:1 1 0;min-width:0;align-items:center;justify-content:center;text-align:center;overflow-wrap:anywhere;padding:8px 10px;background:var(--secondary);color:var(--button-text);text-decoration:none;border:1px solid var(--button-line);border-radius:6px;font-size:13px}
.card-button:hover{background:var(--secondary-hover)}
.empty{color:var(--muted);padding:30px 4px}
.footer{margin-top:38px;color:var(--muted);font-size:12px}
@media(max-width:700px){
  main{padding:20px 14px 42px}
  .toolbar{align-items:stretch;padding:14px;gap:12px}
  .field{flex:1 1 145px;min-width:0}
  .field input[type=number]{width:100%}
  .toggle-field{flex:1 1 100%;align-items:flex-start}
  .toggle-label{min-height:36px}
  .gallery{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
  .card-image{height:clamp(150px,38vw,230px)}
  .card-body{padding:0 10px}
  .card-info{padding:8px;margin:8px 0 10px}
  .card-buttons{flex-direction:column}
  .card-button{flex:1 1 auto}
}
@media(max-width:380px){
  main{padding:16px 10px 32px}
  h1{font-size:25px}
  .gallery{grid-template-columns:minmax(0,1fr)}
  .card-image{height:230px}
  .toolbar{padding:12px}
}
</style>
</head>
<body>
<main>
  <h1>FaceSearch</h1>
  <p class="subtitle">Поиск похожих лиц в текущей базе. Выберите фото, при необходимости укажите лицо на снимке и запустите поиск.</p>
  <section class="toolbar" aria-label="Параметры поиска">
    <div class="field">
      <label for="similarity">Минимальное сходство, %</label>
      <input id="similarity" type="number" min="0" max="100" step="1" value="__DEFAULT_MIN_SIMILARITY__">
    </div>
    <div class="field">
      <label for="top-k">Количество результатов</label>
      <input id="top-k" type="number" min="1" max="500" step="1" value="__DEFAULT_TOP_K__">
    </div>
    <div class="toggle-field">
      <label class="toggle-label" for="use-crop"><input id="use-crop" type="checkbox" checked> Использовать CROP</label>
      <span class="hint">Показывать локальные вырезки лиц, если они доступны.</span>
    </div>
    <div class="field" style="min-width:0">
      <label>&nbsp;</label>
      <button type="button" id="theme-button" class="secondary" aria-pressed="false">☀️ Дневная тема</button>
    </div>
    <div class="field" style="min-width:0">
      <label>&nbsp;</label>
      <button type="button" id="detect-button" class="secondary">Определить лица</button>
    </div>
    <span class="hint">Поддерживаются JPG, PNG, WEBP и другие форматы изображений.</span>
  </section>

  <section id="dropzone" class="dropzone">
    <div class="dropzone-content" id="drop-copy">
      <strong>Перетащите фотографию сюда</strong><br>
      <span class="hint">или выберите файл на компьютере. Можно также вставить изображение из буфера обмена.</span>
      <input id="file-input" type="file" accept="image/*">
    </div>
    <canvas id="preview-canvas" aria-label="Предпросмотр. Нажмите на рамку нужного лица, чтобы выбрать его."></canvas>
  </section>
  <div id="status" class="status" role="status">Выберите изображение, затем нажмите «Определить лица».</div>
  <button type="button" id="search-button" disabled>Искать похожее лицо</button>

  <section>
    <div class="section-title"><h2>Результаты</h2><span id="result-count"></span></div>
    <div id="gallery" class="gallery"><div class="empty">Здесь появятся найденные совпадения.</div></div>
  </section>
  <div class="footer">Процент сходства — мера визуальной близости embeddings, а не подтверждение личности. Доступ к серверу следует ограничивать доверенной сетью.</div>
</main>
<script>
(function(){
  const fileInput=document.getElementById("file-input");
  const dropzone=document.getElementById("dropzone");
  const dropCopy=document.getElementById("drop-copy");
  const canvas=document.getElementById("preview-canvas");
  const ctx=canvas.getContext("2d");
  const statusBox=document.getElementById("status");
  const detectButton=document.getElementById("detect-button");
  const searchButton=document.getElementById("search-button");
  const gallery=document.getElementById("gallery");
  const resultCount=document.getElementById("result-count");
  const similarityInput=document.getElementById("similarity");
  const topKInput=document.getElementById("top-k");
  const useCropCheckbox=document.getElementById("use-crop");
  const themeButton=document.getElementById("theme-button");
  let dataUrl="";
  let imageObject=null;
  let queryId="";
  let faces=[];
  let selectedFace=-1;
  let busy=false;

  function applyTheme(theme){
    const value=theme==="light"?"light":"dark";
    document.documentElement.dataset.theme=value;
    document.documentElement.style.colorScheme=value;
    themeButton.textContent=value==="dark"?"☀️ Дневная тема":"🌙 Ночная тема";
    themeButton.setAttribute("aria-pressed",String(value==="light"));
    try{localStorage.setItem("facesearch-http-theme",value);}catch(e){}
  }
  let initialTheme="dark";
  try{initialTheme=localStorage.getItem("facesearch-http-theme")||"dark";}catch(e){}
  applyTheme(initialTheme);
  themeButton.addEventListener("click",function(){
    applyTheme(document.documentElement.dataset.theme==="dark"?"light":"dark");
  });

  function faceImageUrl(faceId,useCrop,refresh){
    return "/api/face/"+encodeURIComponent(faceId)+"/image?crop="+(useCrop?"1":"0")+(refresh?"&refresh="+Date.now():"");
  }
  function refreshCardImages(){
    gallery.querySelectorAll("img[data-face-image='true']").forEach(function(image){
      const box=image.parentElement;
      const placeholder=box&&box.querySelector(".no-image");
      image.style.display="block";
      if(placeholder)placeholder.hidden=true;
      image.src=faceImageUrl(image.dataset.faceId,useCropCheckbox.checked,true);
    });
  }
  useCropCheckbox.addEventListener("change",refreshCardImages);

  function setStatus(message,isError){
    statusBox.textContent=message;
    statusBox.classList.toggle("error",Boolean(isError));
  }
  function setBusy(value){
    busy=value;
    detectButton.disabled=value||!dataUrl;
    searchButton.disabled=value||!queryId||selectedFace<0||!faces.length;
    fileInput.disabled=value;
  }
  function useFile(file){
    if(!file||!file.type||!file.type.startsWith("image/")){
      setStatus("Выберите файл изображения.",true);return;
    }
    if(file.size>16*1024*1024){
      setStatus("Файл слишком большой. Максимальный размер — 16 МБ.",true);return;
    }
    const reader=new FileReader();
    reader.onload=function(){
      dataUrl=String(reader.result||"");
      imageObject=new Image();
      imageObject.onload=function(){
        queryId="";faces=[];selectedFace=-1;
        canvas.width=imageObject.naturalWidth;
        canvas.height=imageObject.naturalHeight;
        ctx.clearRect(0,0,canvas.width,canvas.height);
        ctx.drawImage(imageObject,0,0);
        canvas.style.display="block";dropCopy.style.display="none";
        searchButton.disabled=true;
        gallery.replaceChildren();
        const empty=document.createElement("div");empty.className="empty";
        empty.textContent="После поиска здесь появятся совпадения.";
        gallery.appendChild(empty);resultCount.textContent="";
        setStatus("Фотография загружена. Нажмите «Определить лица».");
        setBusy(false);
      };
      imageObject.onerror=function(){setStatus("Не удалось открыть изображение.",true);};
      imageObject.src=dataUrl;
    };
    reader.onerror=function(){setStatus("Не удалось прочитать файл.",true);};
    reader.readAsDataURL(file);
  }
  fileInput.addEventListener("change",function(){useFile(fileInput.files&&fileInput.files[0]);});
  ["dragenter","dragover"].forEach(function(name){
    dropzone.addEventListener(name,function(event){event.preventDefault();dropzone.classList.add("drag");});
  });
  ["dragleave","drop"].forEach(function(name){
    dropzone.addEventListener(name,function(event){event.preventDefault();dropzone.classList.remove("drag");});
  });
  dropzone.addEventListener("drop",function(event){
    const file=event.dataTransfer&&event.dataTransfer.files&&event.dataTransfer.files[0];
    if(file)useFile(file);
  });
  document.addEventListener("paste",function(event){
    const items=event.clipboardData&&event.clipboardData.items;
    if(!items)return;
    for(let i=0;i<items.length;i++){
      if(items[i].type&&items[i].type.startsWith("image/")){
        const file=items[i].getAsFile();if(file)useFile(file);event.preventDefault();return;
      }
    }
  });

  function drawFaces(){
    if(!imageObject)return;
    ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.drawImage(imageObject,0,0);
    const scale=Math.max(1,canvas.width/450);
    faces.forEach(function(face,index){
      const b=face.bbox;
      ctx.lineWidth=Math.max(2,2.5*scale);
      ctx.strokeStyle=index===selectedFace?"#4dffad":"#4d9cf5";
      ctx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);
      const label="Лицо "+(index+1)+" · "+Math.round(face.confidence*100)+"%";
      ctx.font=(14*scale)+"px sans-serif";
      const padding=5*scale, labelHeight=22*scale, labelWidth=ctx.measureText(label).width+padding*2;
      const y=Math.max(0,b[1]-labelHeight);
      ctx.fillStyle=index===selectedFace?"#147a4b":"#1763a5";
      ctx.fillRect(b[0],y,labelWidth,labelHeight);
      ctx.fillStyle="#ffffff";ctx.fillText(label,b[0]+padding,y+16*scale);
    });
  }
  canvas.addEventListener("click",function(event){
    if(!faces.length)return;
    const rect=canvas.getBoundingClientRect();
    const x=(event.clientX-rect.left)*canvas.width/rect.width;
    const y=(event.clientY-rect.top)*canvas.height/rect.height;
    let best=-1,bestArea=Infinity;
    faces.forEach(function(face,index){
      const b=face.bbox;
      if(x>=b[0]&&x<=b[2]&&y>=b[1]&&y<=b[3]){
        const area=(b[2]-b[0])*(b[3]-b[1]);
        if(area<bestArea){best=index;bestArea=area;}
      }
    });
    if(best>=0){
      selectedFace=best;drawFaces();setBusy(false);
      setStatus("Выбрано лицо "+(best+1)+". Можно запускать поиск.");
    }
  });

  async function postJson(url,payload){
    const response=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    let data={};
    try{data=await response.json();}catch(e){throw new Error("Сервер вернул некорректный ответ.");}
    if(!response.ok)throw new Error(data.error||("Ошибка HTTP "+response.status));
    return data;
  }
  detectButton.addEventListener("click",async function(){
    if(!dataUrl||busy)return;
    setBusy(true);setStatus("Определение лиц...");
    try{
      const data=await postJson("/api/detect",{image:dataUrl});
      queryId=data.query_id||"";faces=data.faces||[];
      selectedFace=faces.length?0:-1;drawFaces();
      if(!faces.length){
        queryId="";setStatus("Лица не обнаружены. Попробуйте другое изображение.");return;
      }
      setStatus("Обнаружено лиц: "+faces.length+". Нажмите на рамку нужного лица, затем запустите поиск.");
    }catch(error){setStatus(error.message||String(error),true);queryId="";faces=[];selectedFace=-1;}
    finally{setBusy(false);}
  });

  function safeLink(value){
    if(!value)return "";
    try{const url=new URL(value,window.location.href);return (url.protocol==="http:"||url.protocol==="https:")?url.href:"";}
    catch(e){return "";}
  }
  const LOCAL_INFO_LABELS={
    filename:"Файл",date_taken:"Дата съёмки",date_digitized:"Дата оцифровки",
    file_modified:"Изменён",image_width:"Ширина",image_height:"Высота",
    file_size_bytes:"Размер файла",camera_make:"Производитель камеры",
    camera_model:"Камера",lens_model:"Объектив",exposure_time:"Выдержка",
    f_number:"Диафрагма f/",iso:"ISO",focal_length_mm:"Фокусное расстояние, мм",
    artist:"Автор",copyright:"Авторские права",description:"Описание",software:"ПО"
  };
  function displayValue(value){
    if(value===null||value===undefined)return "";
    if(typeof value==="object"){
      try{return JSON.stringify(value);}catch(e){return String(value);}
    }
    return String(value);
  }
  function addInfoBox(body,name,info,isLocal){
    const entries=Object.entries(info&&typeof info==="object"?info:{}).filter(function(pair){
      return pair[1]!==null&&pair[1]!==undefined&&String(displayValue(pair[1])).trim()!=="";
    });
    if(!name&&!entries.length)return;
    const box=document.createElement("div");box.className="card-info";
    if(name){
      const heading=document.createElement("div");heading.className="info-name";
      heading.textContent=name;box.appendChild(heading);
    }
    const ordered=isLocal?entries.sort(function(a,b){
      const ai=Object.keys(LOCAL_INFO_LABELS).indexOf(a[0]);
      const bi=Object.keys(LOCAL_INFO_LABELS).indexOf(b[0]);
      return (ai<0?999:ai)-(bi<0?999:bi);
    }):entries;
    ordered.forEach(function(pair){
      const key=pair[0],value=pair[1];
      if(isLocal&&!Object.prototype.hasOwnProperty.call(LOCAL_INFO_LABELS,key))return;
      const row=document.createElement("div");row.className="card-info-row";
      const label=document.createElement("b");
      label.textContent=(isLocal?LOCAL_INFO_LABELS[key]:key)+": ";
      const text=document.createElement("span");
      let rendered=displayValue(value);
      if(isLocal&&key==="file_size_bytes"){
        const size=Number(value);
        if(Number.isFinite(size))rendered=size>=1048576?(size/1048576).toFixed(2)+" МБ":(size/1024).toFixed(1)+" КБ";
      }else if(isLocal&&(key==="image_width"||key==="image_height")){
        rendered+=" px";
      }
      text.textContent=rendered;row.append(label,text);box.appendChild(row);
    });
    body.appendChild(box);
  }
  function renderResults(results){
    gallery.replaceChildren();
    resultCount.textContent=results.length?"Совпадений: "+results.length:"Совпадений не найдено";
    if(!results.length){
      const empty=document.createElement("div");empty.className="empty";
      empty.textContent="Нет результатов выше заданного порога сходства.";gallery.appendChild(empty);return;
    }
    results.forEach(function(item){
      const faceId=String(item.face_id||"");
      const imported=item.source_type==="import";
      const displayName=String(item.display_name||"");
      const card=document.createElement("article");card.className="card";
      const imageBox=document.createElement("div");imageBox.className="card-image";
      const rank=document.createElement("div");rank.className="rank";rank.textContent="#"+String(item.rank||"");
      const score=document.createElement("div");score.className="score";score.textContent=Number(item.similarity||0).toFixed(2)+"%";
      const image=document.createElement("img");image.loading="lazy";image.dataset.faceImage="true";image.dataset.faceId=faceId;
      image.alt=displayName||( "Лицо ID "+faceId);
      const placeholder=document.createElement("div");placeholder.className="no-image";placeholder.textContent="Изображение недоступно";placeholder.hidden=true;
      image.src=faceImageUrl(faceId,useCropCheckbox.checked,false);
      image.onerror=function(){image.style.display="none";placeholder.hidden=false;};
      imageBox.append(image,placeholder,rank,score);
      const body=document.createElement("div");body.className="card-body";
      const title=document.createElement("div");title.className="card-title";
      title.textContent=imported?(displayName||"Без имени"):("#"+faceId.padStart(8,"0"));
      const meta=document.createElement("div");meta.className="card-meta";
      meta.textContent="Результат №"+item.rank+" · ID лица "+faceId+(item.filename?" · "+item.filename:"");
      body.append(title,meta);

      if(imported){
        addInfoBox(body,displayName,item.info||{},false);
      }else{
        addInfoBox(body,item.metadata_name||"",item.metadata||{},true);
      }

      const source=safeLink(item.source_url||item.profile_url);
      if(source){
        const siteRow=document.createElement("div");siteRow.className="source-site";
        let host=source;
        try{host=new URL(source).hostname;}catch(e){}
        const favicon=document.createElement("img");favicon.className="favicon";favicon.alt="";favicon.loading="lazy";
        try{favicon.src=new URL("/favicon.ico",source).href;}catch(e){favicon.style.display="none";}
        favicon.onerror=function(){favicon.style.display="none";};
        const link=document.createElement("a");link.href=source;link.target="_blank";link.rel="noopener noreferrer";
        link.textContent=host;link.title=source;
        siteRow.append(favicon,link);body.appendChild(siteRow);
      }

      const buttons=document.createElement("div");buttons.className="card-buttons";
      if(imported&&source){
        const sourceButton=document.createElement("a");sourceButton.className="card-button";
        sourceButton.href=source;sourceButton.target="_blank";sourceButton.rel="noopener noreferrer";sourceButton.textContent="Источник";
        buttons.appendChild(sourceButton);
      }
      const originalButton=document.createElement("a");originalButton.className="card-button";
      originalButton.href="/api/face/"+encodeURIComponent(faceId)+"/image?crop=0";
      originalButton.target="_blank";originalButton.rel="noopener noreferrer";originalButton.textContent="Открыть оригинал";
      buttons.appendChild(originalButton);body.appendChild(buttons);
      card.append(imageBox,body);gallery.appendChild(card);
    });
  }
  searchButton.addEventListener("click",async function(){
    if(!queryId||selectedFace<0||busy)return;
    const minimum=Number(similarityInput.value);
    const topK=Number(topKInput.value);
    if(!Number.isFinite(minimum)||minimum<0||minimum>100){
      setStatus("Минимальное сходство должно быть от 0 до 100%.",true);return;
    }
    if(!Number.isFinite(topK)||topK<1||topK>500){
      setStatus("Количество результатов должно быть от 1 до 500.",true);return;
    }
    setBusy(true);setStatus("Поиск похожих лиц...");
    try{
      const data=await postJson("/api/search",{
        query_id:queryId,face_index:selectedFace,
        min_similarity:minimum,top_k:Math.trunc(topK)
      });
      renderResults(data.results||[]);
      setStatus("Поиск завершён. Найдено результатов: "+(data.results||[]).length+".");
    }catch(error){setStatus(error.message||String(error),true);}
    finally{setBusy(false);}
  });
})();
</script>
</body>
</html>
"""


class FaceSearchRequestHandler(BaseHTTPRequestHandler):
    server_version = "FaceSearchHTTP/1.0"

    def log_message(self, fmt, *args):
        print("[HTTP] " + (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob: https:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict):
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Некорректный Content-Length.") from exc
        if length <= 0:
            raise ValueError("Пустое тело запроса.")
        if length > MAX_BODY_BYTES:
            raise OverflowError("Запрос слишком большой.")
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Ожидается корректный JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Ожидается JSON-объект.")
        return data

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            page = PAGE_TEMPLATE.replace(
                "__DEFAULT_TOP_K__", str(DEFAULT_TOP_K)
            ).replace(
                "__DEFAULT_MIN_SIMILARITY__", str(DEFAULT_MIN_SIMILARITY)
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path.startswith("/api/face/") and path.endswith("/image"):
            query = parse_qs(urlparse(self.path).query)
            use_crop = query.get("crop", ["1"])[0] != "0"
            self._serve_face_image(path, use_crop=use_crop)
            return
        if path == "/api/status":
            self._json(200, {
                "ok": True, "profile": PROFILE_NAME, "backend": BACKEND,
                "index_exists": Path(face_search.INDEX_FILE).is_file(),
            })
            return
        self._json(404, {"error": "Страница не найдена."})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/detect", "/api/search"):
            self._json(404, {"error": "API-метод не найден."})
            return
        if not API_SEMAPHORE.acquire(timeout=15):
            self._json(503, {"error": "Сервер перегружен. Повторите запрос через несколько секунд."})
            return
        try:
            try:
                data = self._read_json()
            except OverflowError as exc:
                self._json(413, {"error": str(exc)})
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            if path == "/api/detect":
                self._detect(data)
            else:
                self._search(data)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            print(f"[HTTP][ERROR] {exc}")
            self._json(500, {"error": message[:1000]})
        finally:
            API_SEMAPHORE.release()

    def _detect(self, data: dict):
        image = _decode_image(data.get("image", ""))
        found = face_search._detect_faces_with_padding(
            FACE_APP, image, padding_ratio=0.40
        )
        faces = []
        embeddings = []
        for face in found:
            if float(face.det_score) < face_search.DET_THRESHOLD:
                continue
            embedding = face_search.np.asarray(
                face.normed_embedding, dtype=face_search.np.float32
            ).copy()
            if float(face_search.np.linalg.norm(embedding)) <= 0:
                continue
            bbox = [float(value) for value in face.bbox.tolist()]
            embeddings.append(embedding)
            faces.append({
                "index": len(faces),
                "bbox": bbox,
                "confidence": float(face.det_score),
            })

        token = secrets.token_urlsafe(24)
        with QUERY_SESSIONS_LOCK:
            QUERY_SESSIONS[token] = {
                "created": time.monotonic(),
                "embeddings": embeddings,
            }
        _prune_sessions()
        self._json(200, {"query_id": token, "faces": faces})

    def _search(self, data: dict):
        token = str(data.get("query_id") or "")
        try:
            face_index = int(data.get("face_index", -1))
            top_k = max(1, min(500, int(data.get("top_k", DEFAULT_TOP_K))))
            minimum_percent = float(data.get("min_similarity", DEFAULT_MIN_SIMILARITY))
        except (TypeError, ValueError) as exc:
            raise ValueError("Параметры поиска имеют неверный формат.") from exc
        if not 0.0 <= minimum_percent <= 100.0:
            raise ValueError("Минимальное сходство должно быть от 0 до 100%.")
        if not token or len(token) > 128:
            raise ValueError("Сессия запроса не найдена. Загрузите изображение ещё раз.")

        with QUERY_SESSIONS_LOCK:
            query = QUERY_SESSIONS.get(token)
            if query is None or time.monotonic() - query["created"] > QUERY_TTL_SECONDS:
                QUERY_SESSIONS.pop(token, None)
                query = None
            if query is not None and 0 <= face_index < len(query["embeddings"]):
                embedding = query["embeddings"][face_index].copy()
            else:
                embedding = None
        if embedding is None:
            raise ValueError("Сессия истекла или лицо не выбрано. Определите лица повторно.")

        conn = _open_connection()
        try:
            results = face_search.search_embedding(
                conn,
                embedding,
                top_k=top_k,
                min_similarity=minimum_percent / 100.0,
                backend=BACKEND,
            )
        finally:
            conn.close()

        cards = []
        for result in results:
            face_id = int(result.get("face_id") or 0)
            source_type = str(result.get("source_type") or "local")
            source_url = str(result.get("source_url") or result.get("profile_url") or "")
            url_image = str(result.get("url_image") or "").strip()
            raw_meta = {}
            metadata_doc = {}
            metadata_info = {}
            metadata_name = ""
            try:
                value = json.loads(str(result.get("raw_json") or ""))
                if isinstance(value, dict):
                    raw_meta = value
            except (TypeError, ValueError):
                pass
            if not source_url:
                source_url = str(raw_meta.get("url_source") or "")
            if not url_image:
                url_image = str(raw_meta.get("url_image") or "").strip()

            display_name = str(raw_meta.get("name") or result.get("first_name") or "").strip()
            profile_info = raw_meta.get("info") if isinstance(raw_meta.get("info"), dict) else {}
            try:
                value = json.loads(str(result.get("metadata_json") or ""))
                if isinstance(value, dict):
                    metadata_doc = value
            except (TypeError, ValueError):
                pass
            if (
                set(metadata_doc.keys()) == {"url_source", "name", "url_image", "info"}
                and isinstance(metadata_doc.get("info"), dict)
            ):
                metadata_info = metadata_doc["info"]
                metadata_name = str(metadata_doc.get("name") or "")
            else:
                metadata_info = metadata_doc
                metadata_name = str(metadata_doc.get("filename") or "")
            if source_type == "import":
                if not display_name:
                    display_name = "Без имени"
            else:
                display_name = ""

            cards.append({
                "rank": int(result.get("rank") or len(cards) + 1),
                "face_id": face_id,
                "filename": str(result.get("filename") or ""),
                "similarity": round(float(result.get("score") or 0.0) * 100.0, 2),
                "source_url": source_url,
                "profile_url": str(result.get("profile_url") or ""),
                "url_image": url_image,
                "source_type": source_type,
                "display_name": display_name,
                "info": profile_info,
                "metadata_name": metadata_name,
                "metadata": metadata_info,
            })
        self._json(200, {"results": cards, "count": len(cards)})

    def _serve_face_image(self, route: str, *, use_crop: bool = True):
        parts = route.strip("/").split("/")
        try:
            face_id = int(parts[2])
        except (IndexError, ValueError):
            self._json(400, {"error": "Некорректный ID лица."})
            return
        if face_id <= 0:
            self._json(400, {"error": "Некорректный ID лица."})
            return

        conn = _open_connection()
        try:
            row = conn.execute(
                """
                SELECT i.id, i.path, i.filename, i.source_url, i.url_image
                FROM faces f
                JOIN images i ON i.id = f.image_id
                WHERE f.id = ?
                """,
                (face_id,),
            ).fetchone()
        finally:
            conn.close()

        if not row:
            self._json(404, {"error": "Изображение не найдено."})
            return
        image_id, stored_path, filename, source_url, url_image = row
        crop_path = face_search._resolve_db_crop_path(
            stored_path, image_id=int(image_id)
        )
        if use_crop and crop_path.is_file():
            try:
                body = crop_path.read_bytes()
                content_type = mimetypes.guess_type(str(crop_path))[0] or "image/jpeg"
                self._send(200, body, content_type)
                return
            except OSError:
                pass

        # Если CROP не создавался/утерян, показываем сохранённый оригинал.
        local_source = face_search._source_url_to_local_path(source_url)
        if local_source is not None and local_source.is_file():
            try:
                self._send(
                    200, local_source.read_bytes(),
                    mimetypes.guess_type(str(local_source))[0] or "application/octet-stream",
                )
                return
            except OSError:
                pass

        remote_url = str(url_image or "").strip()
        if remote_url.lower().startswith(("http://", "https://")):
            self._send(302, b"", "text/plain; charset=utf-8", {"Location": remote_url})
            return
        self._json(404, {"error": "CROP отсутствует, исходное изображение недоступно."})


class FaceSearchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Многопоточный HTTP-сервер поиска лиц FaceSearch")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес привязки; по умолчанию только локальный компьютер")
    parser.add_argument("--port", type=int, default=8765, help="HTTP-порт (по умолчанию 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер автоматически")
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть в диапазоне 1–65535.")

    _initialize_runtime()
    server = FaceSearchHTTPServer((args.host, args.port), FaceSearchRequestHandler)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{args.port}/"

    print()
    print("FaceSearch HTTP-сервер запущен.")
    print(f"Локальный адрес: {url}")
    print(f"Профиль БД: {PROFILE_NAME}")
    if args.host in ("0.0.0.0", "::"):
        print("ВНИМАНИЕ: сервер доступен по сети и не имеет встроенной авторизации.")
        print("Не публикуйте порт в Интернет; ограничьте доступ правилами firewall.")
    print("Для остановки нажмите Ctrl+C.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nОстановка HTTP-сервера...")
    finally:
        server.server_close()
        with QUERY_SESSIONS_LOCK:
            QUERY_SESSIONS.clear()


if __name__ == "__main__":
    main()
