// Loaded in <head>, before first paint: marks the page as scripted so the stylesheet can hide
// the browser's own file-picker chrome (in the browser's language, not the page's) without a
// flash of it first. Everything that needs more than this class lives in the other files.
document.documentElement.classList.add("js");
