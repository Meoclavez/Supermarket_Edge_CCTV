import UserNotifications
import Foundation

class NotificationService: UNNotificationServiceExtension {

    var contentHandler: ((UNNotificationContent) -> Void)?
    var bestAttemptContent: UNMutableNotificationContent?

    override func didReceive(_ request: UNNotificationRequest, withContentHandler contentHandler: @escaping (UNNotificationContent) -> Void) {
        self.contentHandler = contentHandler
        bestAttemptContent = (request.content.mutableCopy() as? UNMutableNotificationContent)

        guard let bestAttemptContent = bestAttemptContent else {
            contentHandler(request.content)
            return
        }

        // Extract media URL from APNs payload (snapshot or short video clip)
        let userInfo = request.content.userInfo
        guard let mediaUrlString = (userInfo["snapshot_url"] as? String) ?? (userInfo["clip_url"] as? String),
              let mediaUrl = URL(string: mediaUrlString) else {
            contentHandler(bestAttemptContent)
            return
        }

        // Download the media attachment asynchronously
        let task = URLSession.shared.downloadTask(with: mediaUrl) { (temporaryFileLocation, response, error) in
            if let error = error {
                print("Failed to download push attachment: \(error.localizedDescription)")
                contentHandler(bestAttemptContent)
                return
            }

            guard let temporaryFileLocation = temporaryFileLocation else {
                contentHandler(bestAttemptContent)
                return
            }

            // Determine file extension (.jpg or .mp4)
            let fileExtension = mediaUrl.pathExtension.isEmpty ? "jpg" : mediaUrl.pathExtension
            let localDirectory = URL(fileURLWithPath: NSTemporaryDirectory())
            let localTargetUrl = localDirectory.appendingPathComponent(UUID().uuidString).appendingPathExtension(fileExtension)

            do {
                try FileManager.default.moveItem(at: temporaryFileLocation, to: localTargetUrl)
                let attachment = try UNNotificationAttachment(identifier: "cctv_media", url: localTargetUrl, options: nil)
                bestAttemptContent.attachments = [attachment]
            } catch {
                print("Error creating UNNotificationAttachment: \(error)")
            }

            contentHandler(bestAttemptContent)
        }
        task.resume()
    }

    override func serviceExtensionTimeWillExpire() {
        // Fallback if background download times out
        if let contentHandler = contentHandler, let bestAttemptContent = bestAttemptContent {
            contentHandler(bestAttemptContent)
        }
    }
}
