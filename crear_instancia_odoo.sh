#!/bin/bash

# ==========================================================
# Script: crear_instancia_odoo.sh
# Crea manifiestos de Kubernetes para una nueva instancia de Odoo
# en un clúster K3s con Traefik y Azure PostgreSQL compartido.
#
# Uso: ./crear_instancia_odoo.sh <nombre_instancia> <dominio> <version_odoo>
# Ejemplo: ./crear_instancia_odoo.sh multipago multipago.sdnbo.net 18
# ==========================================================

# No olvidar las variables de entorno:
# export CF_API_TOKEN="your_token"
# export CF_ACCOUNT_ID="your_account_id"
# export CF_ZONE_ID="your_zone_id"
# export CF_TUNNEL_ID="your_tunnel_id"
# export AZURE_PG_HOST="patroni-db.kube-system.svc.cluster.local"  ← HAProxy VIP (always the leader)
# export AZURE_PG_USER="odoo"
# export AZURE_PG_PASSWORD="your_password"

# --- 1. Validaciones iniciales ---
if [ -z "$1" ] || [ -z "$2" ] || [ -z "$3" ]; then
    echo "=========================================================="
    echo "Error: Faltan parámetros."
    echo "Uso: $0 <nombre_instancia> <dominio> <version_odoo>"
    echo "Ejemplo 1: $0 multipago multipago.sdnbo.net 18"
    echo "Ejemplo 2: $0 sintesis erp.sintesis-sa.com 17"
    echo "Versiones soportadas: 17, 18, 19"
    echo "=========================================================="
    exit 1
fi

# Validar versión de Odoo
ODOO_VERSION=$3
if [[ "$ODOO_VERSION" != "17" && "$ODOO_VERSION" != "18" && "$ODOO_VERSION" != "19" ]]; then
    echo "Error: Versión de Odoo no soportada: $ODOO_VERSION"
    echo "Versiones soportadas: 17, 18, 19"
    exit 1
fi

# Validar variables de Azure PostgreSQL
if [ -z "$AZURE_PG_HOST" ] || [ -z "$AZURE_PG_USER" ] || [ -z "$AZURE_PG_PASSWORD" ]; then
    echo "=========================================================="
    echo "Error: Faltan variables de entorno de Azure PostgreSQL."
    echo "Variables requeridas:"
    echo "  export AZURE_PG_HOST=\"aeisoftwaredb.postgres.database.azure.com\""
    echo "  export AZURE_PG_USER=\"odoo\""
    echo "  export AZURE_PG_PASSWORD=\"your_password\""
    echo "=========================================================="
    exit 1
fi

# Validar variables de Cloudflare (opcional)
if [ -z "$CF_API_TOKEN" ] || [ -z "$CF_ACCOUNT_ID" ] || [ -z "$CF_ZONE_ID" ] || [ -z "$CF_TUNNEL_ID" ]; then
    echo "ADVERTENCIA: Faltan variables de Cloudflare. Se omitirá la automatización DNS/Tunnel."
    SKIP_CLOUDFLARE=true
fi

INSTANCE_NAME=$1
DOMAIN=$2
# Sanitizar nombre para K8s (solo minúsculas, números y guiones)
K8S_NAME=$(echo "$INSTANCE_NAME" | tr '_' '-' | tr '[:upper:]' '[:lower:]')
NAMESPACE="odoo-${K8S_NAME}"
CNAME_NAME=$(echo "$DOMAIN" | cut -d'.' -f1)

# --- 2. Creación de la estructura de directorios ---
# Guardar directorio del script ANTES de hacer cd
SCRIPT_DIR=$(cd "$(dirname "$0")" && pwd)
BASE_DIR="k8s-${K8S_NAME}"
echo "[*] Creando directorio: $BASE_DIR"
mkdir -p "$BASE_DIR"
cd "$BASE_DIR" || exit

# --- 3. Generar contraseña maestra ---
ADMIN_PASSWORD=$(openssl rand -base64 24)

# --- 4. Namespace ---
echo "[*] Generando manifiesto: 00-namespace.yaml"
cat <<EOF > 00-namespace.yaml
apiVersion: v1
kind: Namespace
metadata:
  name: ${NAMESPACE}
  labels:
    app: odoo
    client: ${K8S_NAME}
    odoo-version: "${ODOO_VERSION}"
EOF

# --- 5. Secret (credenciales de Azure PostgreSQL) ---
echo "[*] Generando manifiesto: 01-secret.yaml"
cat <<EOF > 01-secret.yaml
apiVersion: v1
kind: Secret
metadata:
  name: ${K8S_NAME}-db-secret
  namespace: ${NAMESPACE}
type: Opaque
stringData:
  db_host: "${AZURE_PG_HOST}"
  db_port: "5432"
  db_user: "${AZURE_PG_USER}"
  db_password: "${AZURE_PG_PASSWORD}"
EOF

# --- 6. ConfigMap (odoo.conf) ---
echo "[*] Generando manifiesto: 02-configmap.yaml"
cat <<EOF > 02-configmap.yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${K8S_NAME}-odoo-conf
  namespace: ${NAMESPACE}
data:
  odoo.conf: |
    [options]
    ; Base de Datos (Azure PostgreSQL compartido)
    db_host = ${AZURE_PG_HOST}
    db_port = 5432
    db_user = ${AZURE_PG_USER}
    db_password = ${AZURE_PG_PASSWORD}
    db_name = ${K8S_NAME}
    db_filter = ${K8S_NAME}

    ; Contraseña maestra
    admin_passwd = ${ADMIN_PASSWORD}

    ; Rutas
    addons_path = /mnt/extra-addons,/usr/lib/python3/dist-packages/odoo/addons
    data_dir = /var/lib/odoo

    ; Rendimiento y Proxy
    workers = 2
    max_cron_threads = 1
    gevent_port = 8072
    proxy_mode = True

    ; Límites
    limit_memory_hard = 2684354560
    limit_memory_soft = 2147483648
    limit_request = 8192
    limit_time_cpu = 600
    limit_time_real = 1200
EOF

# --- 7. PVC (almacenamiento persistente con CephFS RWX) ---
# Both PVCs use ceph-cephfs (ReadWriteMany) so pods can move freely between
# workers without RBD lock contention. RollingUpdate (zero downtime) works.
echo "[*] Generando manifiesto: 03-pvc.yaml"
cat <<EOF > 03-pvc.yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${K8S_NAME}-odoo-data
  namespace: ${NAMESPACE}
  annotations:
    description: "Odoo filestore — CephFS (ReadWriteMany, zero RBD lock contention)"
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ceph-cephfs
  resources:
    requests:
      storage: 10Gi
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${K8S_NAME}-odoo-addons
  namespace: ${NAMESPACE}
  annotations:
    description: "Custom addons — CephFS (ReadWriteMany, shared across all workers)"
spec:
  accessModes:
    - ReadWriteMany
  storageClassName: ceph-cephfs
  resources:
    requests:
      storage: 5Gi
EOF

# --- 8. Deployment ---
echo "[*] Generando manifiesto: 04-deployment.yaml (Odoo ${ODOO_VERSION})"
cat <<EOF > 04-deployment.yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${K8S_NAME}-odoo
  namespace: ${NAMESPACE}
  labels:
    app: odoo
    client: ${K8S_NAME}
    odoo-version: "${ODOO_VERSION}"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: odoo
      client: ${K8S_NAME}
  template:
    metadata:
      labels:
        app: odoo
        client: ${K8S_NAME}
        odoo-version: "${ODOO_VERSION}"
    spec:
      securityContext:
        fsGroup: 101    # odoo user — ensures Ceph RBD volume is writable
      containers:
      - name: odoo
        image: odoo:${ODOO_VERSION}
        ports:
        - containerPort: 8069
          name: http
        - containerPort: 8072
          name: websocket
        env:
        - name: HOST
          valueFrom:
            secretKeyRef:
              name: ${K8S_NAME}-db-secret
              key: db_host
        - name: PORT
          valueFrom:
            secretKeyRef:
              name: ${K8S_NAME}-db-secret
              key: db_port
        - name: USER
          valueFrom:
            secretKeyRef:
              name: ${K8S_NAME}-db-secret
              key: db_user
        - name: PASSWORD
          valueFrom:
            secretKeyRef:
              name: ${K8S_NAME}-db-secret
              key: db_password
        volumeMounts:
        - name: odoo-data
          mountPath: /var/lib/odoo
        - name: odoo-addons
          mountPath: /mnt/extra-addons
        - name: odoo-config
          mountPath: /etc/odoo/odoo.conf
          subPath: odoo.conf
        resources:
          requests:
            memory: "512Mi"
            cpu: "250m"
          limits:
            memory: "2Gi"
            cpu: "1000m"
      volumes:
      - name: odoo-data
        persistentVolumeClaim:
          claimName: ${K8S_NAME}-odoo-data
      - name: odoo-addons
        persistentVolumeClaim:
          claimName: ${K8S_NAME}-odoo-addons
      - name: odoo-config
        configMap:
          name: ${K8S_NAME}-odoo-conf
EOF

# --- 9. Service ---
echo "[*] Generando manifiesto: 05-service.yaml"
cat <<EOF > 05-service.yaml
apiVersion: v1
kind: Service
metadata:
  name: ${K8S_NAME}-odoo-svc
  namespace: ${NAMESPACE}
spec:
  selector:
    app: odoo
    client: ${K8S_NAME}
  ports:
  - name: http
    port: 8069
    targetPort: 8069
  - name: websocket
    port: 8072
    targetPort: 8072
EOF

# --- 10. Ingress (Traefik) ---
echo "[*] Generando manifiesto: 06-ingress.yaml"
cat <<EOF > 06-ingress.yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: ${K8S_NAME}-odoo-ingress
  namespace: ${NAMESPACE}
  annotations:
    traefik.ingress.kubernetes.io/router.entrypoints: web
    traefik.ingress.kubernetes.io/custom-request-headers: "X-Forwarded-Proto: https"
spec:
  rules:
  - host: ${DOMAIN}
    http:
      paths:
      - path: /websocket
        pathType: Prefix
        backend:
          service:
            name: ${K8S_NAME}-odoo-svc
            port:
              number: 8072
      - path: /
        pathType: Prefix
        backend:
          service:
            name: ${K8S_NAME}-odoo-svc
            port:
              number: 8069
EOF

# --- 11. Automatización de Cloudflare ---
if [ "$SKIP_CLOUDFLARE" != "true" ]; then
    echo "[*] Automatizando configuración en Cloudflare..."
    python3 "$SCRIPT_DIR/cloudflare_provision.py" \
        --hostname "$DOMAIN" \
        --service-url "http://traefik.kube-system.svc.cluster.local:80" \
        --cname-name "$CNAME_NAME"
    if [ $? -eq 0 ]; then
        echo "[✓] Cloudflare configurado correctamente."
    else
        echo "[✗] Error al configurar Cloudflare. Revisa los logs."
    fi
fi

# --- 12. Resumen ---
echo "=========================================================="
echo "¡Manifiestos para $INSTANCE_NAME creados con éxito!"
echo ""
echo "  Directorio:     $PWD"
echo "  Namespace:      $NAMESPACE"
echo "  Dominio:        $DOMAIN"
echo "  Versión Odoo:   $ODOO_VERSION"
echo "  Imagen Docker:  odoo:${ODOO_VERSION}"
echo "  Base de Datos:  $AZURE_PG_HOST (db: ${K8S_NAME})"
echo "  Admin Password: $ADMIN_PASSWORD"
echo ""
echo "  Guarda la contraseña maestra en un lugar seguro."
echo ""
echo "  Para desplegar en K3s:"
echo "  kubectl apply -f $PWD/"
echo ""
echo "  Para eliminar la instancia:"
echo "  kubectl delete ns $NAMESPACE"
echo "=========================================================="
