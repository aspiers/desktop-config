// ==UserScript==
// @name        Always login on CI sites
// @description    Automatically fill out login form and submit it
// @namespace   bmwiedemann
// @match          https://ci.suse.de/*
// @match          https://ci.opensuse.org/*
// @version     1
// @grant       none
// @author      Bernhard M. Wiedemann
// ==/UserScript==

'use strict';

function checkForSignIn () {
    var form = document.getElementsByName('login')[0];
    var logindiv = document.querySelector('div.login');
    if (logindiv && !form) {
        var href=logindiv.childNodes[1].href;
	if(href && href.match("/login")) {
	    window.location.replace(href);
	    return;
	}
    }

    if (! form) {
        console.debug("No login form; presumably already logged in.");
        return;
    }
    
    if (! form.action.match("j_acegi_security_check")) {
        console.warn("Unexpected action " + form.action + " for login form!");
        return;
    }

    var uid = form.querySelector('input#j_username');
    if (! uid) {
        console.warn("CI login form missing username field?!");
        return;
    }
    if (! uid.value || uid.value === "") {
        console.info("CI login form missing username; can't auto-login. " +
                     "Will check again in 2s.");
        setTimeout(checkForSignIn, 2000);
        return;
    }

    var pw = document.getElementsByName('j_password')[0];
    if (! pw) {
        console.warn("CI login form missing password field?!");
        return;
    }
    if (! pw.value || pw.value === "") {
        console.info("CI login form missing password; can't auto-login. " +
                     "Will check again in 2s.");
        setTimeout(checkForSignIn, 2000);
        return;
    }

    form.submit();
}

checkForSignIn();
