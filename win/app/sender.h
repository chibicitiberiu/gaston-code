/* "Send something the user just asked for, and say so if it did not go."
 *
 * One interface for the five modules that send user-initiated frames: the
 * option menus, the chat list, the permission bar, the page bar and the
 * compose box. Each used to declare its own identical function-pointer
 * typedef for main.cpp's ui_send, which had no context and so could only
 * reach the connection and the transcript through globals. MainWindow
 * implements it.
 *
 * A failure is reported to the user by the implementation -- which of the
 * two sentences depends on why it failed -- so a PERMREPLY or a SWITCH that
 * never reaches the bridge is reported through the same path as every other
 * user-initiated send.
 */
#ifndef RC_SENDER_H
#define RC_SENDER_H

#ifndef __cplusplus
#error "app/sender.h is C++"
#endif

class FrameSender
{
public:
    virtual ~FrameSender() {}

    /* `what` names the action for the message shown if it cannot go, e.g.
     * "The model change". True if the frame reached the outbound queue. */
    virtual bool sendFrame(const char *what, const char *type,
                           const char *const *keys, const char *const *vals,
                           int nkv, const void *body, unsigned bodyLen) = 0;
};

#endif
