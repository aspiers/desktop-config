// ==UserScript==
// @name           Always login on OBS sites
// @description    Automatically log in on OBS sites
// @match          https://build.opensuse.org/*
// @match          https://build.suse.de/*
// @downloadURL    https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/Always%20login%20on%20OBS%20sites.user.js
// @grant          none
// @version        0.1
// @author         Adam Spiers
// ==/UserScript==
/* jshint -W097 */
'use strict';

function checkForSignIn () {
    var form = document.querySelector('form#login_form');

    if (! form) {
        console.debug("No login form; presumably already logged in.");
        return;
    }
    if (! form.action.match("^https://build\.(opensuse\.org|suse.de)/ICSLogin/")) {
        console.warn("Unexpected action " + form.action + " for login form!");
        return;
    }

    var uid = form.querySelector('input#username');
    if (! uid) {
        console.warn("OBS login form missing username field?!");
        return;
    }
    if (! uid.value || uid.value === "") {
        console.info("OBS login form missing username; can't auto-login. " +
                     "Will check again in 1s.");
        setTimeout(checkForSignIn, 1000);
        return;
    }

    var pw  = form.querySelector('input#password');
    if (! pw) {
        console.warn("OBS login form missing password field?!");
        return;
    }
    if (! pw.value || pw.value === "") {
        console.info("OBS login form missing password; can't auto-login. " +
                     "Will check again in 1s.");
        setTimeout(checkForSignIn, 1000);
        return;
    }

    form.submit();
}

checkForSignIn();
