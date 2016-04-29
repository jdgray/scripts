#!/bin/sh
# Restarts server after deployment
# written by J. Gray

#restart php
sudo service php-fpm restart

#restart nginx
sudo /etc/init.d/nginx restart

#set uploads permissions
sudo chmod 777 /var/www/frontier/current/wp-content/uploads

#copy config
cp /var/www/frontier/wp-config.php /var/www/frontier/current/wp-config.php
