package ru.avtoves.driver;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;

/** Системные уведомления о заказах. */
final class Notifier {
    static final String CHANNEL = "orders";

    private Notifier() {
    }

    static void ensureChannel(Context context) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        if (manager.getNotificationChannel(CHANNEL) == null) {
            manager.createNotificationChannel(
                    new NotificationChannel(CHANNEL, "Заказы", NotificationManager.IMPORTANCE_HIGH));
        }
    }

    static void show(Context context, String title, String text) {
        ensureChannel(context);
        Intent open = new Intent(context, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent tap = PendingIntent.getActivity(context, 0, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        Notification notification = new Notification.Builder(context, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_dialog_info)
                .setContentTitle(title)
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(tap)
                .setAutoCancel(true)
                .build();
        try {
            context.getSystemService(NotificationManager.class)
                    .notify((int) (System.currentTimeMillis() % Integer.MAX_VALUE), notification);
        } catch (SecurityException ignored) {
            // водитель не разрешил уведомления
        }
    }
}
