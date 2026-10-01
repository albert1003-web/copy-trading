package com.tracker;

import java.io.IOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.nio.file.Files;
import java.nio.file.Path;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class TrackerApplication {

    static final int PORT = 8787;
    static final String URL = "http://localhost:" + PORT;

    public static void main(String[] args) throws IOException {
        // Single instance: if the app is already running, just bring up the UI.
        if (!portIsFree(PORT)) {
            Browser.open(URL);
            return;
        }

        String dbPath = System.getenv().getOrDefault("TRACKER_DB_PATH",
                System.getProperty("user.home") + "/TradeTracker/tracker.db");
        Files.createDirectories(Path.of(dbPath).toAbsolutePath().getParent());

        SpringApplication app = new SpringApplication(TrackerApplication.class);
        app.setHeadless(false); // needed for java.awt.Desktop (browser + Dock quit)
        app.run(args);
    }

    private static boolean portIsFree(int port) {
        try (ServerSocket socket = new ServerSocket(port, 0, InetAddress.getLoopbackAddress())) {
            return true;
        } catch (IOException e) {
            return false;
        }
    }
}
