#!/bin/bash

echo "Clearing all jobs from Railway Postgres..."

# Get all job IDs
JOBS=$(curl -s https://robotui.up.railway.app/api/queue | python3 -c "
import sys, json
data = json.load(sys.stdin)
for task in data.get('tasks', []):
    print(task.get('job_id') or task.get('id', ''))
")

if [ -z "$JOBS" ]; then
    echo "No jobs to clear!"
else
    for JOB_ID in $JOBS; do
        echo "Deleting $JOB_ID..."
        curl -s -X DELETE https://robotui.up.railway.app/api/queue/$JOB_ID
        echo " Done!"
    done
fi

echo "All jobs cleared!"
