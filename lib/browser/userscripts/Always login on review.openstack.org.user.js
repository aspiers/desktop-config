// ==UserScript==
// @name         Always login on review.openstack.org
// @namespace    http://adamspiers.org/
// @version      0.1
// @description  Automatically sign in on review.openstack.org
// @author       Adam Spiers
// @match        https://review.openstack.org/*
// @grant        none
// @downloadURL  https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/Always%20login%20on%20review.openstack.org.user.js
// ==/UserScript==
/* jshint -W097 */
'use strict';

function checkForSignIn () {
    if (document.querySelector("span.menuBarUserName")) {
        console.log("Already logged in");
        return;
    }

    var elts = document.querySelectorAll('div.linkMenuBar a');
    if (elts.length == 0) {
        console.log("Document still loading, will check for Sign In button again in 1s");
        setTimeout(checkForSignIn, 1000);
        return;
    }

    // This for loop is easier than trying to get an XPath query
    // like //a[text()[contains(., "Sign In")]] to work without
    // relying on JQuery
    for (var i = 0; i < elts.length; i++) {
        var text = elts[i].text;
        if (text && text.indexOf("Sign In") != -1) {
            console.log("Found Sign In button; clicking ...");
            elts[i].click();
            return;
        }
    }
}

checkForSignIn();