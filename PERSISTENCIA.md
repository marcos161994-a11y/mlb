# Memoria fuera de Git

El estado del experimento vive en Postgres (`DATABASE_URL`). En la PC y en los
tests, si no hay URL, se usa SQLite en `DATA_DIR/memoria.sqlite`.

`memoria_auditoria.json` se queda en el repo como semilla de la primera
importación. La app ya no lo reescribe y el backup de GitHub ya no lo commitea
en `main`. No lo borres hasta comprobar que `/api/health` muestra el historial
en la base.

La interfaz no cambia: el resto del código sigue usando `cargar_memoria()` y
`guardar_memoria()` con el mismo diccionario. Stake, cuotas y panel pueden
añadir claves; se guardan dentro del documento.

## Horas del plan free

Render reparte **750 horas al mes** entre todos los servicios **web** free.
El cron cada 5 minutos mantiene mlb-1 despierto: un solo servicio encendido
todo el mes son unas **720 horas**. Cabe solo si mlb-1 es el único servicio
web free de la cuenta. Neon o Supabase no consumen esas horas.

El disco de un web service free se borra al reiniciar. SQLite ahí no sirve
como archivo definitivo.

## Qué hay que configurar

Servicio vivo: **mlb-1**, https://mlb-1-en7i.onrender.com

1. Crea una base Postgres gratis.
   - Neon: https://neon.tech (plan free). Copia la connection string.
   - Supabase: https://supabase.com (plan free). Usa la URI de Postgres, no la clave `anon`.
   - Render Postgres también vale si ya lo tienes. En cuentas nuevas suele ser de pago. Si lo usas desde mlb-1, pega el **Internal Database URL**.
2. La contraseña va URL-encoded si tiene caracteres raros (`@`, `#`, `%`).
3. En Render → servicio **mlb-1** → Environment:
   - `DATABASE_URL` = esa URI.
   - `CRON_SECRET` = un secreto largo. El mismo valor en GitHub → Settings → Secrets and variables → Actions → secret `CRON_SECRET`.
   - `DATA_DIR` = `/var/data` (ya está en `render.yaml`; no es durable).
4. En GitHub → Settings → Secrets and variables → Actions → Variables:
   - `RENDER_URL` = `https://mlb-1-en7i.onrender.com`
5. No crees otro servicio web free. Suspende cualquiera que no sea mlb-1.
6. Despliega este cambio en mlb-1 (merge a `main`, o deploy manual de la rama).
7. La primera vez que arranque con `DATABASE_URL`, el servidor importa solo el JSON del repo si la base está vacía. También puedes lanzarlo a mano, desde una máquina con el repo y la URL:

```bash
export DATABASE_URL='postgresql://USUARIO:CLAVE@HOST/BASE?sslmode=require'
python migrar_memoria_db.py
```

El script no borra `memoria_auditoria.json`. Si la base ya tiene días que el JSON no tiene, los conserva. Si el resultado fuera a perder fechas, jugadas o lecciones, no escribe y sale con error.

8. Comprueba:

```bash
curl -fsS https://mlb-1-en7i.onrender.com/api/health
```

Tiene que decir `"backend": "postgres"`, `"durable": true`, `"conectado": true` y un `historial.n_dias` que no sea menor que el del JSON.

9. El workflow **Backup memoria Render** baja `/api/exportar-memoria` (con `CRON_SECRET`), sube el artefacto `memoria-db` y lo publica en la rama `backup/memoria`. No empuja a `main`.
10. **Restore memoria Render** despierta el servicio y fusiona ese dump en la base. Si la rama todavía no existe, usa el JSON del repo.

Los crons pegan `/api/health` con reintentos antes de llamar a la API, y hay un workflow **Despertar Render** unos minutos antes de cada backup, por si el free se durmió.
