// ==UserScript==
// @name           Always login on Attachmate sites
// @description    Automatically fill out login form and submit it
// @include        /https?://(.*\.)?(suse|novell|attachmategroup)\.com/.*/
// @grant          none
// @downloadURL    https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/Always%20login%20on%20Attachmate%20sites.user.js
// @version        0.2
// @author         Dirk Mueller
// ==/UserScript==

var loc = location.href;

// redirect to the green side of life
if (loc.match("^https://bugzilla.novell.com")) {
    window.location.replace(
        loc.replace("https://bugzilla.novell.com",
                    "https://bugzilla.suse.com"));
    return;
}

// login, dammit!
if (loc.match("^https://bugzilla.suse.com")) {
    if (loc.indexOf("?GoAheadAndLogin=") < 0 &&
        document.getElementById("login_link_top")) {
        var new_url = loc;
        if (new_url.indexOf("?") < 0)
            new_url = new_url.concat("?");
        new_url = new_url.replace("?", "?GoAheadAndLogIn=1&");
        window.location.replace(new_url);
        return;
    }
}

if (document.forms[0] &&
    document.forms[0].action.match("^https://login.attachmategroup.com")) {

    if (document.getElementById("username")) {
        document.getElementById("password").value=$password;
        document.getElementById("username").value=$username;
        if (document.forms[0]) {
            document.forms[0].submit();
        }
    }
}


