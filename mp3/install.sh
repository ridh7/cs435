#!/usr/bin/env bash

# Fail on error
set -e

# Fail on unset var usage
set -o nounset

# Get directory containing src folder
# to cd up: "${BASH_SOURCE[0]}" )/../.."
SRC_DIR="$( cd -P "$( dirname "${BASH_SOURCE[0]}" )" && pwd -P )"

MN_DIR="$SRC_DIR"

DASH_VERSION="fb1ed8308"
USE_MIRROR=false
MIRROR_URL="https://uofi.box.com/shared/static"
MPEG_URL="z0u6qcjpwced0by442cvtkduwjh00o3j"
MININET_URL="28dzm3ilykzzx7yuv04dtloefpki20vv"
CHROME_URL="byub4cw219rrmlbsnn7ppv9ogmli8tse"
DASH_URL="y1nvf52006uq3wkkw3de207hg0ldwwou"

BUILD_DIR="$(pwd -P)"
case $BUILD_DIR in
    $SRC_DIR*) BUILD_DIR=$SRC_DIR;;  # current directory is a subdirectory
    *) BUILD_DIR=$BUILD_DIR;;
esac

DIST=Unknown
test -e /etc/debian_version && DIST="Debian"
grep Ubuntu /etc/lsb-release &> /dev/null && DIST="Ubuntu"
if [ "$DIST" = "Ubuntu" ] || [ "$DIST" = "Debian" ]; then
    install='sudo apt-get -y install'
    update='sudo apt-get update'
    remove='sudo apt-get -y remove'
    pkginst='sudo dpkg -i'
else
    echo "Only Ubuntu and Debian supported!"
    exit
fi

ARCH=Unknown
if [ "$(uname -m)" == "x86_64" ]; then
    ARCH=amd64
else
    ARCH=i386
fi


function all {
    printf 'installing all...\n' >&2
    echo "Install dir:" $SRC_DIR
    basic
    mininet
    ryu
    vidsrv
    memcached
}


function ryu {
    echo "Installing Ryu..."
    $update

    sudo pip3 install ryu
}


function mininet {
    echo "Installing mininet..."
    $update
    mkdir -p "$SRC_DIR/ext"
    cd "$SRC_DIR/ext"
    
    if [ $USE_MIRROR = true ]
    then
    curl -L $MIRROR_URL/$MININET_URL --output mininet-master.zip
	unzip -o mininet-master.zip
    mv mininet-master mininet
	rm -f mininet-master.zip
    else
	   git clone https://github.com/mininet/mininet || true
    fi

    # it's OK for this command to throw error  
    ./mininet/util/install.sh -kmnv || true
    cd "$SRC_DIR"
    sudo pip3 install mininet
    $install net-tools
}


function memcached {
    echo "Installing memcached dependencies..."
    $update
    $install --quiet memcached php-memcached php-xmlrpc #mysql-server php5-mysql
}


function vidsrv {
    echo "Installing video dependencies..."
    
    cd "$SRC_DIR"
    mkdir -p resources
    mkdir -p resources/chromedatadir

    cd resources
    echo curl -L $MIRROR_URL/$MPEG_URL --output wwwroot.tar.gz
    curl -L $MIRROR_URL/$MPEG_URL --output wwwroot.tar.gz

    tar xvfz wwwroot.tar.gz
    rm -f wwwroot.tar.gz

    cd "$SRC_DIR"

    # Install dependecy packages
    $update
    $install --quiet php nodejs npm aptitude git libxss1 libappindicator1 \
	libindicator7 gconf-service libgconf-2-4 libnspr4 libpango1.0-0 libcurl4

    chrome=google-chrome-stable_current_"$ARCH".deb

    if [ $USE_MIRROR = true ]
    then
        curl -L $MIRROR_URL/$CHROME_URL --output $chrome
    else
       wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    fi

    sudo dpkg -i $chrome
    rm -f $chrome

    # adding xhost + to boot, in order to pop up the chrome client
    sudo xhost +
    echo "if [ \"\$DISPLAY\" != \"\" ]
then
 xhost +
fi
" | sudo tee -a /etc/profile

    # Install dash-js
    cd "$SRC_DIR"
    mkdir -p "$SRC_DIR/ext"
    cd "$SRC_DIR/ext"

    if [ $USE_MIRROR = true ]
    then
    curl -L $MIRROR_URL/$DASH_URL --output dash.js.zip
	unzip -o dash.js.zip
	rm -f dash.js.zip
    else
	git clone https://github.com/Dash-Industry-Forum/dash.js.git
	cd dash.js
	git checkout development
	git checkout $DASH_VERSION
	cd ..
    fi

    cd dash.js/build
    sudo npm install -g grunt-cli

    # npm -- force http, https hangs (issue #1198)
    sudo npm install --registry http://registry.npmjs.org/ 
    grunt uglify
    #grunt jsdoc  # error in dash.js config
    grunt jshint

    # Copy dash-js to our www directory
    cd "$SRC_DIR/ext"
    cp -R dash.js/* "$SRC_DIR/resources/wwwroot"
}

function usage {
    printf '\nUsage %s [-avmwrih]\n\n' $(basename $0) >&2

    printf 'This script will install packages for Mininet and related\n' >&2
    printf 'experiments.  It should work on Ubuntu 14.04\n\n' >&2

    printf 'options:\n' >&2
    printf -- ' -a: install (A)ll packages\n' >&2
    printf -- ' -v: streaming (V)ideo experiment setup \n' >&2
    printf -- ' -w: memcached (W)eb server experiment setup\n' >&2
    printf -- ' -m: (M)ininet install (with flags -kmnvw)\n' >&2
    printf -- ' -r: install (R)yu controller\n' >&2
    printf -- ' -i: use m(i)rror\n' >&2
    printf -- ' -h: print this (H)elp message\n\n' >&2
}

function basic {
    $install git python3 python3-pip curl
    sudo ln -s /usr/bin/python3.8 /usr/bin/python | true
#    sudo update-alternatives --install /usr/bin/python python /usr/bin/python3.8
}

# Check minimum reqs
mem=`grep MemTotal /proc/meminfo | awk '{print $2}'`
if [ $mem -lt 2000000 ]
then
    echo "Not enough memory.  Mimimum requirement: 2GB RAM"
    exit
fi

if [ $# -eq 0 ]
then
    usage
else
    while getopts 'avmwhri' OPTION
    do
	case $OPTION in
	   i) USE_MIRROR=true;;
	esac
    done

    OPTIND=1
    while getopts 'avmwhri' OPTION
    do
	case $OPTION in
	    a) all;;
	    v) vidsrv;;
	    w) memcached;;
	    m) mininet;;
	    r) ryu;;
	    h) usage;;
	esac
    done
    shift $(($OPTIND - 1))
fi

