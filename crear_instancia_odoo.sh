#!/bin/bash

# Uso del script: ./crear_instancia_odoo.sh ticketeg erp.ticketeg.com
# No es necesario agregar puerto, el nuevo proxy reverso "Traefik" sabe como direccionar el tráfico al contenedor correcto observando la url del dominio de accceso.
# Tampoco es necesario configurar cloudflare por que existe un registro DNS  de tipo Cname con un comodin que reenvia todos los subdominios de sdnbo.net al tunel correcto.

# En Cloudflare DNS:
# Ve a tu zona DNS (sdnbo.net).
# Crea un nuevo registro CNAME.
# Nombre: * (el asterisco es el comodín).
# Target: El ID de tu túnel (ej. [ID-DE-TU-TUNEL].cfargotunnel.com).
# Proxy status: Proxied (Nube naranja).
#
# En Cloudflare Tunnel (Dashboard):
# En lugar de crear múltiples "Public Hostnames", crea uno solo que atrape todo:
# Public Hostname: *.sdnbo.net
# Service: http://localhost:80 (Asumiendo que Traefik escucha en el puerto 80 del host).
# Configuración Adicional: Asegúrate de que WebSockets esté habilitado en la pestaña Network del túnel.



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

INSTANCE_NAME=$1
DOMAIN=$2
BASE_DIR="odoo-$INSTANCE_NAME"

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

echo "=========================================================="
echo "¡Entorno para $INSTANCE_NAME creado con éxito!"
echo "Directorio: $PWD"
echo "Dominio asignado: $DOMAIN"
echo "Contraseña Maestra (admin_passwd): $ADMIN_PASSWORD"
echo "Guarda esta contraseña maestra en un lugar seguro."
echo "Para arrancar la instancia, ejecuta:"
echo "cd $BASE_DIR && sudo docker compose up -d"
echo "=========================================================="
