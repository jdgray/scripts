#!/bin/bash 
title="booklaunch"
now=`date +"%m_%d_%Y"`

echo "Starting mongo backup..."

#lock mongo
mongo -u admin -p thepassword admin --eval "printjson(db.fsyncLock())"

#create snapshots

#data
data=$(aws ec2 create-snapshot --volume-id vol-3d36a33c --description "$title-data_$now")
echo $data
#journal
journal=$(aws ec2 create-snapshot --volume-id vol-c937a2c8 --description "$title-journal_$now")
echo $journal
#log
log=$(aws ec2 create-snapshot --volume-id vol-1436a315 --description "$title-log_$now")
echo $log

#unlock mongo
mongo -u admin -p thepassword admin --eval "printjson(db.fsyncUnlock())"

#todo
#delete old snapshots
#aws ec2 describe-snapshots | grep booklaunch | sort -r -k 5 | sed 1,6d | awk '{print "Deleting snapshot: " $2}; system("aws ec2 delete-snapshot " $2)'

echo "Finished mongo backup!"



