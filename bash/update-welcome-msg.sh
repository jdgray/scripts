$cat ~/.bash_profile
hname=`hostname`
echo "Welcome on $hname."

echo -e "Kernel Details: " `uname -smr`
echo -e "`bash --version`"
echo -ne "Uptime: "; uptime
echo -ne "Server time : "; date
