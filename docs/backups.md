# Backups

`election-backup.timer` runs a backup every six hours (00:02, 06:02, 12:02 and 18:02
UTC). Each backup is a bundle and the packs it names:

- The **bundle** holds a PostgreSQL dump of the whole history, the files rewritten in
  place (collector state, `runs/*/latest.json`, reports, accuracy and recovery checks),
  the deployment catalog, the code of the current and previous releases, and a manifest.
- **Packs** hold the files written once: raw snapshots, fetch metadata, parsed output,
  run reports and publication artifacts. A backup packs only the files new since the
  previous uploaded backup, one pack for raw archives and one for the rest, and uploads
  them once to `<prefix>/objects/packs/`. They are never expired.
- The **manifest** maps every write-once file to its hash, size and pack. A file the
  previous manifest lists with the same size and modification time is not read again.
  With no previous bundle on disk (the first run, or one lost), every file is packed.

The newest backup therefore restores every snapshot an older one does; older ones only
add earlier states of the system. Bundles made before packs (manifest version 1) hold
every file themselves and still restore.

A backup with nothing new to hold is skipped (`--skip-unchanged`, on in the timer's
service). Each upload records a fingerprint of what a backup exists to keep: the raw
snapshots, publications and restore checks on disk, the snapshot and publication rows,
and the deployment catalog. Every new result arrives as a new raw snapshot. When the
fingerprint is unchanged and the last upload finished less than a day ago, the run writes
`latest-backup-unchanged.json` instead and the last upload stays the latest backup.
Poll logs (run reports, collector state, fetch and load attempts) change on every poll
and are left out, so a skip loses at most a day of them, never a result. While states
are collected this rarely skips: Mississippi alone saves a new county manifest every
poll. Once states are certified and their collection stops, it skips most backups.

## Running alongside collection

A backup pauses collection for a few seconds, not for the whole run. It waits until no
job is running (up to 10 minutes), then holds every source's locks and the exclusive
publication lock just long enough to:

- export the database snapshot;
- copy the files rewritten in place (collector state, `runs/*/latest.json`, reports,
  accuracy and recovery checks);
- list every other file under the data directory.

After that it releases the locks. Packing the new files, the table fingerprints and
the `pg_dump` (which reads that same snapshot) run while jobs run normally. Files written after the snapshot are left for the next backup. A job that
starts during the brief hold finds its source `locked` and is skipped, not failed;
the next cycle runs it.

## Retention

| Where | Kept | How |
| --- | --- | --- |
| Server disk | The newest uploaded bundle (its manifest is the next backup's index) | `prune_backups.py --keep 1` after each run |
| S3 bundles | Every backup under 3 days old, and always the newest 5 | Tags plus a lifecycle rule, below |
| S3 packs | All, indefinitely | Never tagged; any backup may name an old pack |

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

## Restoring

`production_recovery.py check-restore --backup-id <id>` downloads the bundle and checks
that every pack its manifest names is in S3 at its recorded size. It extracts every raw
pack, verifying each file's hash, and of the other packs the newest and one chosen at
random. It then restores the dump into a temporary database and checks it as before.

For a real restore, `production_recovery.py fetch --backup-id <id>` downloads the
bundle and every pack it names, verifies every file, and prints the directory holding
the rebuilt `data/`, the dump and the code.

## One-time AWS setup

Run with an administrator profile, not the backup user.

1. The backup user writes, reads and lists `election-pipeline/objects/` as well as
   `election-pipeline/rolling/`, and never deletes. Allow it to tag backup objects. Add
   to its policy, with your bucket:

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
`MAX_BACKUP_AGE_SECONDS` (25200, seven hours, in `monitor.env`) or did not upload, unless
a backup within that time was skipped as unchanged against that same upload and the
upload is less than a day plus that limit old.
