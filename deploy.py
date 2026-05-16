#!/usr/bin/env python3
import os
import sys
import subprocess
import logging
import shutil
from pathlib import Path

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

def run_cmd(cmd, sudo=False, check=True, shell=False):
    """Утилита для безопасного запуска системных команд"""
    if sudo and os.geteuid() != 0:
        cmd = f"sudo {cmd}" if shell else ["sudo"] + cmd

    try:
        result = subprocess.run(cmd, check=check, text=True, capture_output=True, shell=shell)
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        logging.error(f"Ошибка выполнения: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
        logging.error(f"Вывод ошибки: {e.stderr}")
        if check:
            sys.exit(1)
        return None

def prepare_rhel10_environment():
    """Проверка ОС и автоматическая подготовка окружения Podman для RHEL 10"""
    logging.info("Проверка операционной системы...")

    is_rhel10 = False
    if os.path.exists("/etc/os-release"):
        with open("/etc/os-release", "r") as f:
            os_info = f.read()
            if "el10" in os_info or "platform:el10" in os_info or "10" in os_info:
                is_rhel10 = True

    if not is_rhel10:
        logging.info("Дистрибутив отличается от RHEL 10. Пропускаем специфичные для Podman настройки.")
        return

    logging.info("Обнаружена RHEL 10 / CentOS Stream 10. Настраиваем окружение Podman...")

    # 1. Удаляем Docker CE, если он мешает, и ставим Podman
    if not shutil.which("podman") or not shutil.which("docker-compose"):
        logging.info("Установка подсистемы Podman и компонентов совместимости (podman-docker)...")
        run_cmd(["dnf", "remove", "-y", "docker-ce", "docker-ce-cli", "containerd.io"], sudo=True, check=False)
        run_cmd(["dnf", "install", "-y", "podman", "podman-docker", "podman-compose"], sudo=True)

    # 2. Настройка реестров, чтобы короткие имена искались на Docker Hub
    reg_file = Path("/etc/containers/registries.conf")
    if reg_file.exists():
        content = reg_file.read_text()
        if "docker.io" not in content:
            logging.info("Добавление Docker Hub в список доверенных реестров...")
            append_config = '\nunqualified-search-registries = ["docker.io", "quay.io"]\n'
            run_cmd(f"echo '{append_config}' | sudo tee -a {reg_file}", shell=True)

    # 3. Активация пользовательского сокета Podman для работы Compose плагина
    logging.info("Настройка и запуск пользовательского сокета Podman...")
    current_user = os.environ.get('USER', 'ec2-user')

    run_cmd(["sudo", "loginctl", "enable-linger", current_user], check=False)
    run_cmd(["systemctl", "--user", "enable", "--now", "podman.socket"], check=False)

    user_uid = os.getuid()
    os.environ["DOCKER_HOST"] = f"unix:///run/user/{user_uid}/podman/podman.sock"

def generate_docker_compose(selected_tools, target_dir="./"):
    """Генерация файла docker-compose.yml и конфигураций"""
    logging.info("Генерация конфигурационных файлов...")

    config_dir = Path(target_dir) / "config"
    data_dir = Path(target_dir) / "data"
    prom_data_dir = data_dir / "prometheus"
    grafana_data_dir = data_dir / "grafana"

    config_dir.mkdir(parents=True, exist_ok=True)
    prom_data_dir.mkdir(parents=True, exist_ok=True)
    grafana_data_dir.mkdir(parents=True, exist_ok=True)

    prom_config_path = config_dir / "prometheus.yml"
    if prom_config_path.is_dir():
        shutil.rmtree(prom_config_path)

    # Конфигурация Prometheus
    if "prometheus" in selected_tools:
        base_prom_yaml = """global:
  scrape_interval: 15s

scrape_configs:
  - job_name: 'prometheus'
    static_configs:
      - targets: ['localhost:9090']
"""
        if "node-exporter" in selected_tools:
            base_prom_yaml += """
  - job_name: 'node-exporter'
    static_configs:
      - targets: ['node-exporter:9100']
"""
        # [ИЗМЕНЕНИЕ 1] Автоматический сбор метрик с процесс-экспортера, если он выбран
        if "process-exporter" in selected_tools:
            base_prom_yaml += """
  - job_name: 'process-exporter'
    static_configs:
      - targets: ['process-exporter:9256']
"""

        with open(prom_config_path, "w") as f:
            f.write(base_prom_yaml)
        logging.info("Файл конфигурации config/prometheus.yml успешно создан.")

    # Настройка прав UID для rootless Podman
    logging.info("Настройка корректных UID прав для rootless-контейнеров...")
    if "prometheus" in selected_tools:
        run_cmd(["podman", "unshare", "chown", "-R", "65534:65534", str(prom_data_dir)], check=False)
        run_cmd(["podman", "unshare", "chown", "65534:65534", str(prom_config_path)], check=False)

    if "grafana" in selected_tools:
        run_cmd(["podman", "unshare", "chown", "-R", "472:472", str(grafana_data_dir)], check=False)

    run_cmd(["chmod", "755", str(config_dir), str(data_dir)], check=False)

    # Сборка манифеста docker-compose.yml
    compose_data = "services:\n"

    if "node-exporter" in selected_tools:
        compose_data += """  node-exporter:
    image: prom/node-exporter:v1.7.0
    container_name: node-exporter
    restart: unless-stopped
    volumes:
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/rootfs:ro
    command:
      - '--path.procfs=/host/proc'
      - '--path.rootfs=/rootfs'
      - '--path.sysfs=/host/sys'
      - '--collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc)($|/)'
    ports:
      - "9100:9100"
\n"""

    if "prometheus" in selected_tools:
        compose_data += """  prometheus:
    image: prom/prometheus:v2.49.1
    container_name: prometheus
    restart: unless-stopped
    volumes:
      - ./config/prometheus.yml:/etc/prometheus/prometheus.yml:Z
      - ./data/prometheus:/prometheus:Z
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
    ports:
      - "9090:9090"
\n"""

    if "grafana" in selected_tools:
        compose_data += """  grafana:
    image: grafana/grafana:10.3.1
    container_name: grafana
    restart: unless-stopped
    volumes:
      - ./data/grafana:/var/lib/grafana:Z
    ports:
      - "3000:3000"
\n"""

    # [ИЗМЕНЕНИЕ 2] Добавлен блок безопасного развертывания process-exporter для Podman + SELinux
    if "process-exporter" in selected_tools:
        compose_data += """  process-exporter:
    image: docker.io/ncabatoff/process-exporter:latest
    container_name: process-exporter
    restart: unless-stopped
    security_opt:
      - label=disable
    volumes:
      - /proc:/host/proc
    command:
      - "-procnames"
      - "python,nginx,deployer"
      - "-procfs"
      - "/host/proc"
    ports:
      - "9256:9256"
\n"""

    compose_path = Path(target_dir) / "docker-compose.yml"
    with open(compose_path, "w") as f:
        f.write(compose_data)

    logging.info("Файл docker-compose.yml успешно сгенерирован.")

def main():
    print("====================================================")
    print("  Автоматический установщик стека мониторинга RHEL10")
    print("====================================================")

    # [ИЗМЕНЕНИЕ 3] Обновлен список доступных инструментов в UI подсказке
    print("Доступны: prometheus, node-exporter, grafana, process-exporter")
    user_input = input("Введите список инструментов через запятую: ")

    selected_tools = [tool.strip().lower() for tool in user_input.split(",") if tool.strip()]

    if not selected_tools:
        logging.error("Не выбрано ни одного инструмента для установки.")
        sys.exit(1)

    prepare_rhel10_environment()
    generate_docker_compose(selected_tools)

    logging.info("Запуск контейнеров через подсистему compose...")
    run_cmd(["docker", "compose", "down"], check=False)
    run_cmd(["docker", "compose", "up", "-d"])
    logging.info("Стек мониторинга успешно развернут и запущен!")

if __name__ == "__main__":
    main()
