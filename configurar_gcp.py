"""
Script para configurar Google Cloud Platform para Vertex AI.
Guía paso a paso para la configuración.
"""

import os
import json

def crear_guia_configuracion():
    """Crea una guía de configuración para GCP"""
    
    guia = """
==================================================
  CONFIGURACIÓN DE GOOGLE CLOUD PLATFORM PARA VERTEX AI
==================================================

PASO 1: Crear proyecto en Google Cloud Platform
-------------------------------------------------
1. Ve a https://console.cloud.google.com/
2. Crea un nuevo proyecto o usa uno existente
3. Copia el ID del proyecto (ej: "my-mlb-project-12345")

PASO 2: Habilitar Vertex AI API
--------------------------------
1. En tu proyecto, ve a "APIs & Services" > "Library"
2. Busca "Vertex AI API"
3. Haz clic en "Enable"

PASO 3: Configurar autenticación
---------------------------------
OPCIÓN A: Usar autenticación personal (recomendada para desarrollo)
1. Instala Google Cloud SDK:
   - Descarga desde: https://cloud.google.com/sdk/docs/install
   - O usa: gcloud init

2. Autentícate:
   gcloud auth application-default login

OPCIÓN B: Usar Service Account (recomendada para producción)
1. Ve a "IAM & Admin" > "Service Accounts"
2. Crea un service account
3. Asigna roles: "Vertex AI User" y "Storage Object Viewer"
4. Descarga la clave JSON
5. Guarda el archivo como "gcp_credentials.json" en este directorio
6. Configura variable de entorno:
   set GOOGLE_APPLICATION_CREDENTIALS=gcp_credentials.json

PASO 4: Actualizar configuración
--------------------------------
1. Abre config_experimento.json
2. Actualiza estos campos:
   - "gcp_project": "TU_ID_DE_PROYECTO"
   - "gcp_location": "us-central1" (o tu región preferida)
   - "usar_ia": true

PASO 5: Verificar configuración
--------------------------------
Ejecuta este script para verificar:
python verificar_gcp.py

==================================================
"""
    
    print(guia)
    
    # Guardar guía en archivo
    with open('GUIA_GCP_CONFIGURACION.txt', 'w', encoding='utf-8') as f:
        f.write(guia)
    
    print("\nGuía guardada en GUIA_GCP_CONFIGURACION.txt")

def verificar_configuracion():
    """Verifica si la configuración de GCP está lista"""
    
    print("\n=== Verificando configuración de GCP ===\n")
    
    # Verificar variables de entorno
    credentials = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if credentials:
        print(f"[OK] GOOGLE_APPLICATION_CREDENTIALS: {credentials}")
    else:
        print("[FALTA] GOOGLE_APPLICATION_CREDENTIALS no configurada")
    
    # Verificar archivo de credenciales
    if os.path.exists('gcp_credentials.json'):
        print("[OK] Archivo gcp_credentials.json encontrado")
    else:
        print("[FALTA] Archivo gcp_credentials.json no encontrado")
    
    # Verificar config_experimento.json
    if os.path.exists('config_experimento.json'):
        with open('config_experimento.json', 'r') as f:
            config = json.load(f)
        
        gcp_project = config.get('gcp_project', '')
        usar_ia = config.get('usar_ia', False)
        
        if gcp_project:
            print(f"[OK] gcp_project configurado: {gcp_project}")
        else:
            print("[FALTA] gcp_project no configurado en config_experimento.json")
        
        if usar_ia:
            print("[OK] usar_ia está activado")
        else:
            print("[FALTA] usar_ia está desactivado")
    else:
        print("[FALTA] config_experimento.json no encontrado")
    
    # Verificar dependencias
    try:
        import vertexai
        print("[OK] vertexai instalado")
    except ImportError:
        print("[FALTA] vertexai no instalado")
    
    try:
        from google.cloud import aiplatform
        print("[OK] google-cloud-aiplatform instalado")
    except ImportError:
        print("[FALTA] google-cloud-aiplatform no instalado")

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--verificar':
        verificar_configuracion()
    else:
        crear_guia_configuracion()
