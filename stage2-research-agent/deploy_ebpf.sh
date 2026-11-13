#!/bin/bash

# 严格模式：出错即退、未定义变量报错；调试输出可通过环境变量控制
set -euo pipefail
if [ "${EBPF_AGENT_DEBUG:-0}" = "1" ]; then
  set -x
fi

# # 确保以 root 身份运行（CD 场景下通过 sudo 或 root 账号执行）
# if [ "$EUID" -ne 0 ]; then
#   echo "请使用 root 身份或通过 sudo 执行本脚本"
#   exit 1
# fi

# 定义路径常量
readonly LOG_DIR="/var/log/uc-paas/ebpf-agent"
readonly AGENT_DIR="/usr/local/bin/ebpf-agent"
readonly TMPFS_DIR="/vdata/uc-paas/ebpf-agent/tmpfs"
readonly PROGS_DIR="/var/lib/ebpf-agent/progs"
readonly CACHE_DIR="/var/lib/ebpf-agent/cache"
readonly BPFTRACE_DIR="/var/lib/ebpf-agent/bpftrace"
readonly CONFIG_PATH="/etc/ebpf-agent"
readonly FILE_RULE="*.data"
readonly OPT_PATH="/opt/ebpf-agent"

# 检查输入参数数量
if [ "$#" -ne 12 ]; then
    echo "Usage: $0 HOSTNAME REGIONID DATA_CONF KAFKA_BROKERS KAFKA_USER KAFKA_PASSWORD ROTATE_CONF LABELSPATH CHDB_ENABLED CHDB_PATH CHDB_DATABASE CHDB_BATCH_SIZE CHDB_FLUSH_INTERVAL"
    exit 1
fi

# 输入参数
readonly HOSTNAME="$1"
readonly REGION_ID="$2"
readonly DATA_CONF="$3"
readonly KAFKA_BROKERS="$4"
readonly KAFKA_USER="$5"
readonly KAFKA_PASSWORD="$6"
readonly ROTATE_CONF="$7"
readonly CHDB_ENABLED="$8"
readonly CHDB_PATH="$9"
readonly CHDB_DATABASE="${10}"
readonly CHDB_BUFFER_SIZE="${11}"
readonly CHDB_MAX_POOL_SIZE="${12}"

# 解析 DATA_CONF
IFS=',' read -ra data_conf_array <<< "$DATA_CONF"
if [ "${#data_conf_array[@]}" -ne 4 ]; then
    echo "Error: DATA_CONF must contain exactly four comma-separated values."
    exit 1
fi
readonly ORIGINAL_TOPIC="${data_conf_array[0]}"
readonly KAFKA_OPT="${data_conf_array[1]}"
readonly NEW_TOPIC="${data_conf_array[2]}"
readonly TMPFS_OPT="${data_conf_array[3]}"

# 解析 KAFKA_BROKERS
IFS=',' read -ra broker_array <<< "$KAFKA_BROKERS"
if [ "${#broker_array[@]}" -lt 1 ]; then
    echo "Error: At least one Kafka broker address must be provided."
    exit 1
fi

# 解析 ROTATE_CONF
IFS=',' read -ra rotate_conf_array <<< "$ROTATE_CONF"
if [ "${#rotate_conf_array[@]}" -ne 3 ]; then
    echo "Error: ROTATE_CONF must contain exactly three comma-separated values."
    exit 1
fi
readonly MAX_SIZE="${rotate_conf_array[0]}"
readonly MAX_BACKUPS="${rotate_conf_array[1]}"
readonly MAX_AGE="${rotate_conf_array[2]}"

# CHDB 开启时校验必填项
if [ "${CHDB_ENABLED}" = "true" ]; then
    if [ -z "${CHDB_PATH}" ] || [ -z "${CHDB_DATABASE}" ]; then
        echo "Error: CHDB 已开启，CHDB_PATH 与 CHDB_DATABASE 不能为空。"
        exit 1
    fi
fi

# 二进制须先于目录创建检查
if [ ! -f "${AGENT_DIR}/ebpf-agent" ]; then
    echo "Error: 二进制文件 ${AGENT_DIR}/ebpf-agent 不存在，请先下发或安装 ebpf-agent。"
    exit 1
fi
chmod u+x "${AGENT_DIR}/ebpf-agent"

# 创建运行目录（CHDB 目录仅在开启时创建）
mkdir -p "$LOG_DIR" "$AGENT_DIR" "$TMPFS_DIR" "$PROGS_DIR" "$CACHE_DIR" "$BPFTRACE_DIR" "$CONFIG_PATH" "$OPT_PATH"
if [ "${CHDB_ENABLED}" = "true" ]; then
    mkdir -p "$CHDB_PATH"
fi

# 配置 agent config 文件
function configure_agent_config() {
    local config_file="${CONFIG_PATH}/config"
    # local config_tmp
    # config_tmp="$(mktemp "${config_file}.XXXXXX")"
    sudo tee "$config_file" <<- EOF
storage:
  kafka:
    enabled: ${KAFKA_OPT}
    groupId: "ebpf-agent"
    autoCommit: true
    autoCommitInterval: 1
    topic: ${ORIGINAL_TOPIC}
    brokers:
$(for broker in "${broker_array[@]}"; do printf '      - %s\n' "$broker"; done)
    sasl:
      enabled: true
      user: "${KAFKA_USER}"
      password: "${KAFKA_PASSWORD}"
      mechanism: SCRAM-SHA-256
  tmpfs:
    enabled: ${TMPFS_OPT}
    path: "$TMPFS_DIR"
    maxSize: ${MAX_SIZE}
    maxBackups: ${MAX_BACKUPS}
    maxAge: ${MAX_AGE}
  chdb:
    enabled: ${CHDB_ENABLED}
    path: "${CHDB_PATH}"
    database: "${CHDB_DATABASE}"
    buffer_size: ${CHDB_BUFFER_SIZE}
    max_pool_size: ${CHDB_MAX_POOL_SIZE}
hostname: "${HOSTNAME}"
server:
  http:
    server: "${HOSTNAME}:38080"
    auth: false
    enable: true
localpath: "$PROGS_DIR"
cachepath: "$CACHE_DIR"
bpftrace: "$BPFTRACE_DIR"
EOF
    # chmod 600 "$config_tmp"
    # mv -f "$config_tmp" "$config_file"
}
configure_agent_config

# 日志采集标签：tmpfs 开启时写入 ebpf-agent.labels，供节点日志采集（fluentbit）读取 tmpfs 产物并投递 Kafka
function configure_labels() {
    sudo tee /usr/lib/systemd/system/ebpf-agent.labels <<- EOF
dubhe.welkin/node=ebpfnode
dubhe.welkin/paas_account_id=296337866946
dubhe.welkin/paas_instance_name=paas-ebpf
dubhe.welkin/paas_namespace=ebpf
dubhe.welkin/paas_node_name=ebpfnode
dubhe.welkin/cluster=ebpf-cluster
dubhe.welkin/managed-by=ebpf
dubhe.welkin/host=${HOSTNAME}
dubhe.welkin/paas_product_id=ebpf
dubhe.welkin/paas_node_type=master
dubhe.welkin/paas_region_id=${REGION_ID}
dubhe.welkin/paas_systemd_log=node
dubhe.welkin/paas_file_log={"localPath":[{"logType":"product","hostPath":"${TMPFS_DIR}","logFileRule":"${FILE_RULE}","logDeploy":"ebpf","kafkaTopic":"${NEW_TOPIC}","multilinesStartRule":"\\\n","multilinesEndRule":"\\\n"}],"kafkaBroker":"${KAFKA_BROKERS}","kafkaUsername":"${KAFKA_USER}","kafkaPassword":"${KAFKA_PASSWORD}","kafkaMechanism":"SCRAM-SHA-256","kafkaProtocol":"sasl_plaintext"}
EOF
}
if [ "${TMPFS_OPT}" = "true" ]; then
    configure_labels
else
    echo "tmpfs 未开启，跳过 ebpf-agent.labels 日志采集配置"
fi

# 配置 systemd unit file
function configure_unit_file() {
    sudo tee /usr/lib/systemd/system/ebpf-agent.service <<- EOF
[Unit]
Description=CUCloud eBPF Agent
Wants=network-online.target
After=network-online.target

[Service]
EnvironmentFile=/opt/ebpf-agent/env
ExecStart=/usr/local/bin/ebpf-agent/ebpf-agent  -log_dir="$LOG_DIR"   -logtostderr=false   -secrets=/opt/ebpf-agent/secrets -v=5
User=root
Group=root
Restart=always
RestartSec=30
MemoryMax=32G
CPUQuota=1000%

[Install]
WantedBy=multi-user.target
EOF
}
configure_unit_file

# 设置环境变量
function set_environment_variables() {
    sudo tee /opt/ebpf-agent/env <<- EOF
LD_LIBRARY_PATH=/opt/cufalco/lib
EOF
}
set_environment_variables


# 配置密钥文件
# TODO(安全): 当前账号密码硬编码在脚本中；后续可改为 pipeline 参数或密钥管理服务注入，避免明文落盘到 Git
function configure_secrets() {
    local secrets_file="${OPT_PATH}/secrets"
    # local secrets_tmp
    # secrets_tmp="$(sudo mktemp "${secrets_file}.XXXXXX")"
    # umask 077
    sudo tee "$secrets_file" <<- EOF
admin=ckubmxZtOZ4,superuser
user=ckubmxZtOZ4,user
guest=ckubmxZtOZ4,user
EOF
    # sudo chmod 600 "$secrets_tmp"
    # mv -f "$secrets_tmp" "$secrets_file"
}
configure_secrets

# 重新加载 systemd 并启动服务
function restart_service() {
    local service_name="ebpf-agent.service"
    local max_wait_seconds=30

    sudo systemctl daemon-reload
    sudo systemctl enable "${service_name}"
    sudo systemctl restart "${service_name}"

    echo "等待 ${service_name} 启动（最多 ${max_wait_seconds} 秒）..."
    for (( i=0; i<max_wait_seconds; i++ )); do
        if sudo systemctl is-active --quiet "${service_name}"; then
            echo "ebpf-agent started successfully"
            return 0
        fi
        sleep 1
    done

    echo "Failed to restart ebpf-agent：等待超时，服务未处于 active 状态。"
    sudo systemctl status "${service_name}" --no-pager -l || true
    sudo journalctl -u "${service_name}" -n 30 --no-pager || true
    return 1
}
restart_service