from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import os
from dotenv import load_dotenv

from .database import engine, Base
from .routes import router

load_dotenv()

#Criar tabelas no banco de Dados
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="API NFC-e SEFAZ-PE Restaurante",
    description="Sistema de Gestão com Integração com SEFAZ-PE",
    version="1.0.0",
)

# Configuração de CORS
allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# COnfigurar templates e arquivos estáticos
templates = Jinja2Templates(directory="frontend/templates")
app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

# Incluir rotas da API
app.include_router(router)

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    """Endpoint para renderizar a página inicial"""
    return templates.TemplateResponse("base.html", {"request": request})

@app.get("/health")
async def health_check():
    """Endpoint para verificar a saúde da API"""
    return {"status": "healthy OK"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, reload=True)