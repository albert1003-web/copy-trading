package com.tracker;

import java.awt.Desktop;
import java.net.URI;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import jakarta.annotation.PreDestroy;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.context.event.ApplicationReadyEvent;
import org.springframework.context.ConfigurableApplicationContext;
import org.springframework.context.event.EventListener;
import org.springframework.stereotype.Component;

/**
 * Shows the UI once the server is up.
 *
 * <p>Prefers a standalone app window (Chromium "--app" mode with its own profile: no tabs or address bar).
 * Closing that window quits the app, and quitting the app closes the window. Falls back to a tab in the default browser.
 */
@Component
public class Browser {

    private static final Logger log = LoggerFactory.getLogger(Browser.class);

    private static final List<String> CHROMIUM_BROWSERS = List.of(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
            "/Applications/Chromium.app/Contents/MacOS/Chromium");

    /** A window process that exits faster than this handed off to an already-open window; don't treat it as closed. */
    private static final long HANDOFF_MILLIS = 5_000;

    private final ConfigurableApplicationContext context;
    private volatile Process window;
    private volatile boolean shuttingDown;

    @Value("${tracker.open-browser:true}")
    private boolean openBrowser;

    public Browser(ConfigurableApplicationContext context) {
        this.context = context;
    }

    @EventListener(ApplicationReadyEvent.class)
    public void onReady() {
        log.info("Trade Tracker running at {}", TrackerApplication.URL);
        if (!openBrowser) {
            return;
        }
        window = open(TrackerApplication.URL);
        if (window != null) {
            quitWhenClosed(window);
        }
    }

    /** Quitting from the UI also closes the app window. */
    @PreDestroy
    public void closeWindow() {
        shuttingDown = true;
        if (window != null && window.isAlive()) {
            window.destroy();
        }
    }

    /** Opens the UI; returns the app-window process if one was started, else null. */
    static Process open(String url) {
        try {
            String chromium = CHROMIUM_BROWSERS.stream().filter(p -> Files.isExecutable(Path.of(p))).findFirst().orElse(null);
            if (chromium != null) {
                Path profile = Path.of(System.getProperty("user.home"), "TradeTracker", "window-profile");
                return new ProcessBuilder(chromium,
                        "--app=" + url,
                        "--user-data-dir=" + profile,
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--window-size=1280,860")
                        .redirectErrorStream(true)
                        .redirectOutput(ProcessBuilder.Redirect.DISCARD)
                        .start();
            }
            if (Desktop.isDesktopSupported() && Desktop.getDesktop().isSupported(Desktop.Action.BROWSE)) {
                Desktop.getDesktop().browse(URI.create(url));
            } else {
                new ProcessBuilder("open", url).start();
            }
        } catch (Exception e) {
            log.warn("Could not open the UI; visit {} manually", url, e);
        }
        return null;
    }

    private void quitWhenClosed(Process window) {
        long started = System.currentTimeMillis();
        window.onExit().thenRun(() -> {
            if (shuttingDown) {
                return; // we closed it ourselves
            }
            if (System.currentTimeMillis() - started < HANDOFF_MILLIS) {
                log.info("App window handed off to an existing window; use Quit in the UI to stop");
                return;
            }
            log.info("App window closed; shutting down");
            System.exit(SpringApplication.exit(context));
        });
    }
}
