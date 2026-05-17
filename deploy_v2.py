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
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Глобальная карта доступных инструментов (ТОП-10)
TOOLS_MAP = {
    1: ("Prometheus", "prometheus"),
    2: ("Grafana", "grafana"),
    3: ("Node Exporter (Метрики хоста)", "node-exporter"),
    4: ("Process Exporter (Метрики процессов)", "process-exporter"),
    5: ("Elasticsearch (База логов)", "elasticsearch"),
    6: ("Kibana (Визуализация логов)", "kibana"),
    7: ("Logstash (Конвейер логов)", "logstash"),
    8: ("Filebeat (Сборщик логов)", "filebeat"),
    9: ("cAdvisor (Метрики контейнеров)", "cadvisor"),
    10: ("Alertmanager (Менеджер алертов)", "alertmanager")
}

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
        logging.info("Дистрибутив отличается от RHEL 10. Пропускаем специфичные настройки.")
        return

    logging.info("Обнаружена RHEL 10. Настраиваем окружение Podman...")
    if not shutil.which("podman") or not shutil.which("docker-compose"):
        run_cmd(["dnf", "remove", "-y", "docker-ce", "docker-ce-cli", "containerd.io"], sudo=True, check=False)
        run_cmd(["dnf", "install", "-y", "podman", "podman-docker", "podman-compose"], sudo=True)

    reg_file = Path("/etc/containers/registries.conf")
    if reg_file.exists():
        content = reg_file.read_text()
        if "docker.io" not in content:
            append_config = '\nunqualified-search-registries = ["docker.io", "quay.io"]\n'
            run_cmd(f"echo '{append_config}' | sudo tee -a {reg_file}", shell=True)

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
    config_dir.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)

    # Динамическая сборка prometheus.yml
    if "prometheus" in selected_tools:
        prom_config_path = config_dir / "prometheus.yml"
        base_prom_yaml = "global:\n  scrape_interval: 15s\n\nscrape_configs:\n"
        base_prom_yaml += "  - job_name: 'prometheus'\n    static_configs:\n      - targets: ['localhost:9090']\n"
        
        if "node-exporter" in selected_tools:
            base_prom_yaml += "  - job_name: 'node-exporter'\n    static_configs:\n      - targets: ['node-exporter:9100']\n"
        if "process-exporter" in selected_tools:
            base_prom_yaml += "  - job_name: 'process-exporter'\n    static_configs:\n      - targets: ['process-exporter:9256']\n"
        if "cadvisor" in selected_tools:
            base_prom_yaml += "  - job_name: 'cadvisor'\n    static_configs:\n      - targets: ['cadvisor:8080']\n"

        with open(prom_config_path, "w") as f:
            f.write(base_prom_yaml)
        
        # Права для rootless
        run_cmd(["podman", "unshare", "chown", "-R", "65534:65534", str(data_dir.mkdir(parents=True, exist_ok=True) or data_dir)], check=False)

    # Сборка общего docker-compose.yml
    compose_data = "services:\n"
    
    if "prometheus" in selected_tools:
        compose_data += """  prometheus:
    image: prom/prometheus:v2.49.1
    container_name: prometheus
    restart: unless-stopped
    volumes:
      - ./config/prometheus.yml:/etc/prometheus/prometheus.yml:Z
    ports:
      - "9090:9090"
\n"""

    if "grafana" in selected_tools:
        compose_data += """  grafana:
    image: grafana/grafana:10.3.1
    container_name: grafana
    restart: unless-stopped
    ports:
      - "3000:3000"
\n"""

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
    ports:
      - "9100:9100"
\n"""

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

    if "elasticsearch" in selected_tools:
        compose_data += """  elasticsearch:
    image: docker.io/elastic/elasticsearch:8.12.2
    container_name: elasticsearch
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
    ports:
      - "9200:9200"
    restart: unless-stopped
\n"""

    if "kibana" in selected_tools:
        compose_data += """  kibana:
    image: docker.io/elastic/kibana:8.12.2
    container_name: kibana
    environment:
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
    ports:
      - "5601:5601"
    restart: unless-stopped
\n"""

    if "logstash" in selected_tools:
        compose_data += """  logstash:
    image: docker.io/elastic/logstash:8.12.2
    container_name: logstash
    ports:
      - "5044:5044"
      - "9600:9600"
    restart: unless-stopped
\n"""

    if "filebeat" in selected_tools:
        compose_data += """  filebeat:
    image: docker.io/elastic/filebeat:8.12.2
    container_name: filebeat
    user: root
    volumes:
      - /var/log:/var/log:ro
    restart: unless-stopped
\n"""

    if "cadvisor" in selected_tools:
        compose_data += """  cadvisor:
    image: gcr.io/cadvisor/cadvisor:v0.49.1
    container_name: cadvisor
    privileged: true
    security_opt:
      - label=disable
    volumes:
      - /:/rootfs:ro
      - /var/run:/var/run:ro
      - /sys:/sys:ro
      - /var/lib/containers:/var/lib/docker:ro
    ports:
      - "8080:8080"
    restart: unless-stopped
\n"""

    if "alertmanager" in selected_tools:
        compose_data += """  alertmanager:
    image: prom/alertmanager:v0.27.0
    container_name: alertmanager
    ports:
      - "9093:9093"
    restart: unless-stopped
\n"""

    compose_path = Path(target_dir) / "docker-compose.yml"
    with open(compose_path, "w") as f:
        f.write(compose_data)
    logging.info("Файл docker-compose.yml успешно обновлен.")

def main():
    print("====================================================")
    print("  Установщик инструментов DevOps & Мониторинга (RHEL 10)")
    print("====================================================")
    print("Выберите необходимые компоненты из списка:\n")
    
    for key, val in TOOLS_MAP.items():
        print(f"  [{key}] {val[0]}")
        
    print("\n====================================================")
    user_input = input("Введите номера компонентов через запятую (например: 1, 2, 5): ")
    
    # Парсинг цифирных ответов пользователя
    selected_tools = []
    try:
        choices = [int(num.strip()) for num in user_input.split(",") if num.strip()]
        for choice in choices:
            if choice in TOOLS_MAP:
                selected_tools.append(TOOLS_MAP[choice][1])
    except ValueError:
        logging.error("Критическая ошибка ввода. Пожалуйста, используйте только числа и запятые.")
        sys.exit(1)
        
    if not selected_tools:
        logging.error("Список выбранных инструментов пуст.")
        sys.exit(1)
        
    logging.info(f"Выбранные компоненты: {', '.join(selected_tools)}")
    
    prepare_rhel10_environment()
    generate_docker_compose(selected_tools)
    
    logging.info("Запуск выбранного стека контейнеров...")
    run_cmd(["docker", "compose", "down"], check=False)
    run_cmd(["docker", "compose", "up", "-d"])
    logging.info("Сборка и запуск выбранных контейнеров завершены!")

if __name__ == "__main__":
    main()
