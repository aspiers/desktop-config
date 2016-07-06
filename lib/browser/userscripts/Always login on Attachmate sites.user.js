// ==UserScript==
// @name           Always login on Attachmate sites
// @description    Automatically fill out login form and submit it
// @include        /https?://(.*\.)?(suse|novell|attachmategroup|microfocus)\.com/.*/
// @grant          none
// @downloadURL    https://github.com/aspiers/desktop-config/raw/master/lib/browser/userscripts/Always%20login%20on%20Attachmate%20sites.user.js
// @version        0.3
// @author         Dirk Mueller, Bernhard M. Wiedemann
// ==/UserScript==

function checkForSignIn () {
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

    var form = document.forms[0];
    if (form && form.action.match("^https://login.microfocus.com")) {
	var uid = document.getElementById("username");
	var pw = document.getElementById("password");
	if (! (uid && pw)) {
	    console.warn("bugzilla login form missing username/password field?!");
	    return;
	}
	if (! (uid.value && pw.value) || uid.value === "" || pw.value === "") {
	    console.info("bugzilla login form missing username/password; can't auto-login. " +
			 "Will check again in 2s.");
	    setTimeout(checkForSignIn, 2000);
	    return;
	}
	// needed because submit button has name='submit' which overlays the form submit function
	document.createElement('form').submit.call(form);
    }
}

checkForSignIn();
