#!/bin/bash

# Uso del script: ./crear_instancia_odoo.sh ticketeg erp.ticketeg.com
# No es necesario agregar puerto, el nuevo proxy reverso "Traefik" sabe como direccionar el tráfico al contenedor correcto observando la url del dominio de accceso.
# Tampoco es necesario configurar cloudflare por que existe un registro DNS  de tipo Cname con un comodin que reenvia todos los subdominios de sdnbo.net al tunel correcto.

# No olvidar las variables de entorno de Cloudflare
#export CF_API_TOKEN="your_token"
#export CF_ACCOUNT_ID="your_account_id"
#export CF_ZONE_ID="your_zone_id"
#export CF_TUNNEL_ID="your_tunnel_id"

# --- 1. Validaciones iniciales ---
if [ -z "$1" ] || [ -z "$2" ]; then
    echo "=========================================================="
    echo "Error: Faltan parámetros."
    echo "Uso: $0 <nombre_instancia> <dominio>"
    echo "Ejemplo 1: $0 multipago multipago.sdnbo.net"
    echo "Ejemplo 2: $0 sintesis erp.sintesis-sa.com"
    echo "=========================================================="
    exit 1
fi

# Validar variables de Cloudflare
if [ -z "$CF_API_TOKEN" ] || [ -z "$CF_ACCOUNT_ID" ] || [ -z "$CF_ZONE_ID" ] || [ -z "$CF_TUNNEL_ID" ]; then
    echo "=========================================================="
    echo "ADVERTENCIA: Faltan variables de entorno de Cloudflare."
    echo "Se crearán los archivos locales, pero NO se automatizará DNS/Tunnel."
    echo "Variables requeridas: CF_API_TOKEN, CF_ACCOUNT_ID, CF_ZONE_ID, CF_TUNNEL_ID"
    echo "=========================================================="
    SKIP_CLOUDFLARE=true
fi

INSTANCE_NAME=$1
DOMAIN=$2
BASE_DIR="odoo-$INSTANCE_NAME"
# Extraer el subdominio para el CNAME (asumiendo que es la primera parte del dominio)
CNAME_NAME=$(echo "$DOMAIN" | cut -d'.' -f1)

# --- 2. Creación de la estructura de directorios ---
echo "[*] Creando directorio para la instancia: $INSTANCE_NAME..."
mkdir -p "$BASE_DIR/addons"
cd "$BASE_DIR" || exit

# --- 3. Generación de contraseñas seguras ---
echo "[*] Generando contraseñas seguras..."
# Usa openssl para generar cadenas aleatorias
DB_PASSWORD=$(openssl rand -base64 24)
ADMIN_PASSWORD=$(openssl rand -base64 24)

# Guardar la contraseña de DB en el archivo de secrets
echo "$DB_PASSWORD" > odoo_pg_pass
chmod 644 odoo_pg_pass

# --- 4. Creación del archivo odoo.conf ---
echo "[*] Generando odoo.conf..."
cat << EOF > odoo.conf
[options]
; Configuración de Base de Datos
db_host = ${INSTANCE_NAME}-odoo-db
db_port = 5432
db_user = odoo
db_password = ${DB_PASSWORD}

; Contraseña maestra
admin_passwd = ${ADMIN_PASSWORD}

; Rutas
addons_path = /mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons
data_dir = /var/lib/odoo

; Configuración de Rendimiento y Proxy
workers = 2
max_cron_threads = 1
longpolling_port = 8072
proxy_mode = True

; Límites (Ajustar según el plan de recursos de este cliente)
limit_memory_hard = 2684354560
limit_memory_soft = 2147483648
limit_request = 8192
limit_time_cpu = 600
limit_time_real = 1200
EOF

# --- 5. Creación del docker-compose.yml ---
echo "[*] Generando docker-compose.yml..."
cat << EOF > docker-compose.yml
services:
  web:
    image: odoo:18
    container_name: ${INSTANCE_NAME}-odoo-web
    depends_on:
      - db
    volumes:
      - odoo-web-data:/var/lib/odoo
      - ./odoo.conf:/etc/odoo/odoo.conf
      - ./addons:/mnt/extra-addons
    networks:
      - web-proxy
      - internal
    labels:
      - "traefik.enable=true"
      # Solución a múltiples redes
      - "traefik.docker.network=web-proxy"

      # Middleware Local (Redirección a HTTPS y headers seguros)
      - "traefik.http.middlewares.${INSTANCE_NAME}-headers.headers.customrequestheaders.X-Forwarded-Proto=https"

      # ROUTER PRINCIPAL (HTTP 8069)
      - "traefik.http.routers.${INSTANCE_NAME}.rule=Host(\`${DOMAIN}\`)"
      - "traefik.http.routers.${INSTANCE_NAME}.entrypoints=web"
      - "traefik.http.routers.${INSTANCE_NAME}.service=${INSTANCE_NAME}-svc"
      - "traefik.http.routers.${INSTANCE_NAME}.middlewares=${INSTANCE_NAME}-headers"
      - "traefik.http.services.${INSTANCE_NAME}-svc.loadbalancer.server.port=8069"

      # ROUTER WEBSOCKET (Chat/Video 8072)
      - "traefik.http.routers.${INSTANCE_NAME}-ws.rule=Host(\`${DOMAIN}\`) && PathPrefix(\`/websocket\`)"
      - "traefik.http.routers.${INSTANCE_NAME}-ws.entrypoints=web"
      - "traefik.http.routers.${INSTANCE_NAME}-ws.service=${INSTANCE_NAME}-ws-svc"
      - "traefik.http.routers.${INSTANCE_NAME}-ws.middlewares=${INSTANCE_NAME}-headers"
      - "traefik.http.services.${INSTANCE_NAME}-ws-svc.loadbalancer.server.port=8072"
    environment:
      - PASSWORD_FILE=/run/secrets/postgresql_password
      - ODOO_RC=/etc/odoo/odoo.conf
    secrets:
      - postgresql_password

  db:
    image: postgres:16
    container_name: ${INSTANCE_NAME}-odoo-db
    environment:
      - POSTGRES_DB=postgres
      - POSTGRES_PASSWORD_FILE=/run/secrets/postgresql_password
      - POSTGRES_USER=odoo
      - PGDATA=/var/lib/postgresql/data/pgdata
    volumes:
      - odoo-db-data:/var/lib/postgresql/data/pgdata
    networks:
      - internal
    secrets:
      - postgresql_password

volumes:
  odoo-web-data:
  odoo-db-data:

networks:
  web-proxy:
    external: true
  internal:
    driver: bridge

secrets:
  postgresql_password:
    file: ./odoo_pg_pass
EOF

# --- 6. Automatización de Cloudflare ---
if [ "$SKIP_CLOUDFLARE" != "true" ]; then
    echo "[*] Automatizando configuración en Cloudflare..."
    # Cambiamos temporalmente al directorio raíz para ejecutar el script de python si es necesario,
    # o simplemente lo llamamos con la ruta relativa correcta.
    # Dado que estamos dentro de $BASE_DIR, subimos un nivel.
    python3 ../cloudflare_provision.py --hostname "$DOMAIN" --service-url "http://traefik:80" --cname-name "$CNAME_NAME"
    if [ $? -eq 0 ]; then
        echo "[✓] Cloudflare configurado correctamente."
    else
        echo "[✗] Error al configurar Cloudflare. Revisa los logs."
    fi
fi

echo "=========================================================="
echo "¡Entorno para $INSTANCE_NAME creado con éxito!"
echo "Directorio: $PWD"
echo "Dominio asignado: $DOMAIN"
echo "Contraseña Maestra (admin_passwd): $ADMIN_PASSWORD"
echo "Guarda esta contraseña maestra en un lugar seguro."
echo "Para arrancar la instancia, ejecuta:"
echo "cd $BASE_DIR && sudo docker compose up -d"
echo "=========================================================="
