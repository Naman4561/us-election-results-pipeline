# Backups

`election-backup.timer` runs a full backup every six hours (00:02, 06:02, 12:02 and
18:02 UTC). Each bundle is self-contained: every raw snapshot ever collected, fetch
metadata, collector state, a PostgreSQL dump of the whole history, the deployment
catalog and each pinned release's code. The newest bundle therefore holds every
snapshot an older one does; older bundles only add earlier states of the system.

## Running alongside collection

A backup pauses collection for a few seconds, not for the whole run. It waits until no
job is running (up to 10 minutes), then holds every source's locks and the exclusive
publication lock just long enough to:

- export the database snapshot;
- copy the files rewritten in place (collector state, `runs/*/latest.json`, reports,
  accuracy and recovery checks);
- list every other file under the data directory.

After that it releases the locks. The listed files, the table fingerprints and the
`pg_dump` (which reads that same snapshot) take about 25 minutes while jobs run
normally. Files written after the snapshot are left for the next backup. A job that
starts during the brief hold finds its source `locked` and is skipped, not failed;
the next cycle runs it.

## Retention

| Where | Kept | How |
| --- | --- | --- |
| Server disk | The newest uploaded bundle | `prune_backups.py --keep 1` after each run |
| S3 | Every backup under 3 days old, and always the newest 5 | Tags plus a lifecycle rule, below |

After each successful upload, `production_recovery.py backup` lists
`<prefix>/rolling/` and tags the objects of every backup outside the newest
`BACKUP_S3_KEEP_LATEST` (default 5) with `retention=expire`. Uploads abandoned without a
completion record are tagged once a day old. The bucket's lifecycle rule deletes tagged
objects once they are 3 days old. Nothing is tagged unless a backup was just uploaded,
so if backups stop, the newest five stay indefinitely.

The backup user can tag but not delete. A compromised server could at most mark recent
backups for expiry, and S3 would still wait until they are 3 days old.

A retention failure does not fail the backup (it is already in S3). The run report
records it under `s3_retention`, and the monitor raises `backup/s3-retention`.

## One-time AWS setup

Run with an administrator profile, not the backup user.

1. Allow the backup user to tag backup objects. Add to its policy, with your bucket:

   ```json
   {
     "Effect": "Allow",
     "Action": "s3:PutObjectTagging",
     "Resource": "arn:aws:s3:::YOUR_BUCKET/election-pipeline/rolling/*"
   }
   ```

2. Add the lifecycle rule. `put-bucket-lifecycle-configuration` replaces every rule on
   the bucket, so read the current configuration first and merge.

   ```json
   {
     "Rules": [
       {
         "ID": "expire-tagged-backups",
         "Status": "Enabled",
         "Filter": {
           "And": {
             "Prefix": "election-pipeline/rolling/",
             "Tags": [{ "Key": "retention", "Value": "expire" }]
           }
         },
         "Expiration": { "Days": 3 },
         "NoncurrentVersionExpiration": { "NoncurrentDays": 1 }
       },
       {
         "ID": "abort-incomplete-backup-uploads",
         "Status": "Enabled",
         "Filter": { "Prefix": "election-pipeline/rolling/" },
         "AbortIncompleteMultipartUpload": { "DaysAfterInitiation": 1 }
       }
     ]
   }
   ```

   Lifecycle age counts from each object's upload, not from when it was tagged. S3
   applies rules once a day, so deletion lags by up to a day.

## Monitoring

The monitor flags `backup` when the newest report is older than
`MAX_BACKUP_AGE_SECONDS` (25200, seven hours, in `monitor.env`) or did not upload.
