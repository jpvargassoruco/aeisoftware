FROM odoo:19.0

USER root

# Install system dependencies for Docker tracking
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    curl \
    git \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release && \
    # Install Docker CLI and Compose plugin (for DooD)
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /usr/share/keyrings/docker-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    apt-get update && \
    apt-get install -y docker-ce-cli docker-compose-plugin && \
    # Clean up
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Install Python requirements for cloudflare_manager
# Using requests (cloudflare_manager/client.py relies on it)
RUN pip3 install --no-cache-dir requests --break-system-packages

# Odoo configuration
# Add PYTHONPATH to include cloudflare_manager when Odoo runs
ENV PYTHONPATH="/opt/aeisoftware:${PYTHONPATH}"

# Keep user as root to interact with /var/run/docker.sock
# or we'd need to dynamically map the docker group ID
USER root
