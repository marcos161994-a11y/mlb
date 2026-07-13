"""
Script para desplegar el sistema en Google Cloud Run.
Permite ejecutar el sistema 24/7 sin necesidad de tu PC local.
"""

import os
import subprocess
import json

def crear_dockerfile():
    """Crea Dockerfile para despliegue en Cloud Run"""
    dockerfile = """
FROM python:3.11-slim

WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \\
    gcc \\
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar archivos del proyecto
COPY *.py ./
COPY *.json ./
COPY *.pkl ./

# Exponer puerto
EXPOSE 8000

# Comando de inicio
CMD ["uvicorn", "servidor_mlb:app", "--host", "0.0.0.0", "--port", "8000"]
"""
    
    with open('Dockerfile', 'w') as f:
        f.write(dockerfile)
    
    print("Dockerfile creado")

def crear_app_yaml():
    """Crea configuración para Cloud Run"""
    app_yaml = """
runtime: python
env: flex

runtime_config:
  python_version: 3.11

manual_scaling:
  instances: 1

resources:
  cpu: 1
  memory_gb: 0.5
  disk_size_gb: 10
"""
    
    with open('app.yaml', 'w') as f:
        f.write(app_yaml)
    
    print("app.yaml creado")

def instrucciones_despliegue():
    """Muestra instrucciones para despliegue manual"""
    instrucciones = """
==================================================
  DESPLIEGUE EN GOOGLE CLOUD RUN
==================================================

REQUISITOS:
1. Tener Google Cloud SDK instalado
2. Tener proyecto en Google Cloud (ya tienes: still-summit-323011)
3. Habilitar Cloud Run API

PASOS:

1. Instalar Google Cloud SDK:
   Descarga desde: https://cloud.google.com/sdk/docs/install

2. Autenticarte:
   gcloud auth login
   gcloud config set project still-summit-323011

3. Habilitar APIs:
   gcloud services enable cloudbuild.googleapis.com
   gcloud services enable run.googleapis.com

4. Construir y desplegar:
   gcloud run deploy quantum-mlb \\
     --source . \\
     --platform managed \\
     --region us-central1 \\
     --allow-unauthenticated

5. Obtener URL:
   El comando anterior mostrará la URL del servicio

ALTERNATIVA: Usar Docker local

1. Construir imagen:
   docker build -t quantum-mlb .

2. Ejecutar localmente:
   docker run -p 8000:8000 quantum-mlb

ALTERNATIVA: Servicios de hosting fáciles

1. Render.com (Gratis para servicios web):
   - Conecta tu repositorio GitHub
   - Render detecta automáticamente Python
   - Configura variables de entorno
   - Deploy automático

2. Railway.app ($5/mes):
   - Similar a Render
   - Muy fácil de usar
   - Soporta bases de datos

3. DigitalOcean ($4-6/mes):
   - VPS completo
   - Control total
   - Requiere más configuración

==================================================
"""
    
    print(instrucciones)
    
    with open('INSTRUCCIONES_DESPLEGUE.txt', 'w') as f:
        f.write(instrucciones)
    
    print("Instrucciones guardadas en INSTRUCCIONES_DESPLEGUE.txt")

def main():
    print("=== Preparando despliegue en la nube ===\n")
    
    crear_dockerfile()
    crear_app_yaml()
    instrucciones_despliegue()
    
    print("\n=== Archivos de despliegue creados ===")
    print("Revisa INSTRUCCIONES_DESPLEGUE.txt para opciones de hosting")

if __name__ == "__main__":
    main()
