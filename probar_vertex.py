"""
Script para probar la conexión con Vertex AI y Gemini.
"""

import os
import json

def probar_conexion_vertex_ai():
    """Prueba la conexión con Vertex AI"""
    
    print("=== Probando conexión con Vertex AI ===\n")
    
    # Cargar configuración
    with open('config_experimento.json', 'r') as f:
        config = json.load(f)
    
    gcp_project = config.get('gcp_project', '')
    gcp_location = config.get('gcp_location', 'us-central1')
    
    print(f"Proyecto GCP: {gcp_project}")
    print(f"Ubicación: {gcp_location}")
    print(f"IA activada: {config.get('usar_ia', False)}")
    
    # Verificar credenciales
    credentials_path = os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
    if credentials_path:
        print(f"Credenciales: {credentials_path}")
    elif os.path.exists('gcp_credentials.json'):
        print("Credenciales: gcp_credentials.json encontrado")
        os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'gcp_credentials.json'
    else:
        print("ERROR: No se encontraron credenciales")
        print("Opciones:")
        print("1. Ejecuta: gcloud auth application-default login")
        print("2. O descarga un service account JSON como gcp_credentials.json")
        return False
    
    # Intentar importar e inicializar Vertex AI
    try:
        import vertexai
        from vertexai.generative_models import GenerativeModel
        
        print("\nInicializando Vertex AI...")
        vertexai.init(project=gcp_project, location=gcp_location)
        print("Vertex AI inicializado correctamente")
        
        # Probar modelo Gemini (intentar con diferentes modelos)
        print("\nProbando modelo Gemini...")
        
        # Intentar con gemini-1.5-flash (más reciente y disponible)
        try:
            model = GenerativeModel("gemini-1.5-flash")
            response = model.generate_content("Responde en una palabra: ¿Qué es el béisbol?")
            print(f"Respuesta: {response.text}")
        except Exception as e:
            print(f"Error con gemini-1.5-flash: {e}")
            # Intentar con gemini-pro
            try:
                model = GenerativeModel("gemini-pro")
                response = model.generate_content("Responde en una palabra: ¿Qué es el béisbol?")
                print(f"Respuesta: {response.text}")
            except Exception as e2:
                print(f"Error con gemini-pro: {e2}")
                raise e
        
        print("\n=== IA Contextual funcionando correctamente ===")
        return True
        
    except ImportError as e:
        print(f"ERROR: No se pudo importar vertexai: {e}")
        return False
    except Exception as e:
        print(f"ERROR: No se pudo conectar con Vertex AI: {e}")
        print("\nPosibles causas:")
        print("1. Vertex AI API no está habilitada en el proyecto")
        print("2. Credenciales incorrectas o sin permisos")
        print("3. Proyecto o ubicación incorrectos")
        print("4. Problema de red")
        return False

if __name__ == "__main__":
    probar_conexion_vertex_ai()
