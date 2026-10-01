package com.tracker;

import java.awt.Desktop;
import java.net.URI;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

/** Opens the UI in the default browser once the server is up. */
@Component
public class Browser {

    private static final Logger log = LoggerFactory.getLogger(Browser.class);

    @Value("${tracker.open-browser:true}")
    private boolean openBrowser;

    @EventListener(ApplicationReadyEvent.class)
    public void onReady() {
        log.info("Trade Tracker running at {}", TrackerApplication.URL);
        if (openBrowser) {
            open(TrackerApplication.URL);
        }
    }

    static void open(String url) {
        try {
            if (Desktop.isDesktopSupported() && Desktop.getDesktop().isSupported(Desktop.Action.BROWSE)) {
                Desktop.getDesktop().browse(URI.create(url));
            } else {
                new ProcessBuilder("open", url).start();
            }
        } catch (Exception e) {
            log.warn("Could not open browser; visit {} manually", url, e);
        }
    }
}
