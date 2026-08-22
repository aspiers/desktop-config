;;; monitor-controller-emacs-fonts.el --- Exact monitor font leaf  -*- lexical-binding: t; -*-

(defun monitor-controller-apply-font-height (height)
  "Reload the tracked font family and apply exact planned HEIGHT."
  (unless (and (integerp height) (> height 0) (<= height 1000))
    (error "Invalid monitor-controller font height: %S" height))
  (set-face-attribute 'default nil :family "Hack Nerd Font")
  (set-face-attribute 'default nil :height height)
  height)

(provide 'monitor-controller-emacs-fonts)
;;; monitor-controller-emacs-fonts.el ends here
