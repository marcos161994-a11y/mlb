from openai import OpenAI
from pathlib import Path

# Leer API key
api_key_path = Path(__file__).parent / "openai_api_key.txt"
with open(api_key_path, "r") as f:
    api_key = f.read().strip()

print(f"API Key encontrada: {api_key[:20]}...")

try:
    client = OpenAI(api_key=api_key)
    
    # Probar conexión simple
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "Eres un asistente que responde únicamente con números."},
            {"role": "user", "content": "Responde con el número 0.5"}
        ],
        max_tokens=10,
        temperature=0.3
    )
    
    print(f"✅ Conexión exitosa con OpenAI")
    print(f"Respuesta: {response.choices[0].message.content.strip()}")
    
except Exception as e:
    print(f"❌ Error de conexión: {e}")
