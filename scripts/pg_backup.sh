#!/bin/bash
# Daily PostgreSQL backup for AIVideoTrans
BACKUP_DIR=/opt/aivideotrans/backups
KEEP_DAYS=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_FILE=${BACKUP_DIR}/aivideotrans_${TIMESTAMP}.sql.gz

mkdir -p ${BACKUP_DIR}

# Dump and compress
docker exec aivideotrans-postgres pg_dump -U avt aivideotrans | gzip > ${BACKUP_FILE}

if [ $? -eq 0 ]; then
    echo "[$(date)] Backup OK: ${BACKUP_FILE} ($(du -h ${BACKUP_FILE} | cut -f1))"
else
    echo "[$(date)] Backup FAILED" >&2
fi

# Remove backups older than KEEP_DAYS
find ${BACKUP_DIR} -name 'aivideotrans_*.sql.gz' -mtime +${KEEP_DAYS} -delete
