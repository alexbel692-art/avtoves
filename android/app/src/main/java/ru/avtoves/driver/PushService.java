package ru.avtoves.driver;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

/** Уведомление пришло, когда приложение открыто: показываем его сами (в фоне его показывает система). */
public class PushService extends FirebaseMessagingService {
    @Override
    public void onMessageReceived(RemoteMessage message) {
        RemoteMessage.Notification n = message.getNotification();
        if (n != null) {
            Notifier.show(this, n.getTitle() == null ? "АвтоВес" : n.getTitle(), n.getBody() == null ? "" : n.getBody());
        }
    }
}
