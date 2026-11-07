// Copyright OpenCuria contributors. AGPL-3.0-only.

#import <Foundation/Foundation.h>

NS_ASSUME_NONNULL_BEGIN

/// Where the backend requests of one open app go.
NS_SWIFT_NAME(AppGatewaySession)
@interface OCAppGatewaySession : NSObject

@property (nonatomic, readonly) NSURL *url;
@property (nonatomic, readonly) NSString *token;
@property (nonatomic, readonly) NSString *domain;

- (instancetype)init NS_UNAVAILABLE;

@end

/**
 Routes requests of the open app to processes of its workspace.

 Apps call `http://<process>.opencuria.local[:port]/<path>`. React Native's
 networking (fetch, XMLHttpRequest, images), its WebSocket module and
 `expo/fetch` rewrite only those hosts to `<gateway>/<process>/<port>/<path>`
 on the OpenCuria server and add the app session token, through their
 official hooks. Other hosts go out unchanged. The token stays in native
 code; the app's JavaScript never sees it.
 */
NS_SWIFT_NAME(AppGateway)
@interface OCAppGateway : NSObject

/// The session of a launch request's `gateway` object, or nil without one.
+ (nullable OCAppGatewaySession *)sessionFromJSON:(nullable NSDictionary *)json
    NS_SWIFT_NAME(session(fromJSON:));

/// Route backend requests to `session`, the app that is open now.
+ (void)openSession:(nullable OCAppGatewaySession *)session NS_SWIFT_NAME(open(_:));

/// Stop routing for `session`; a session opened after it stays.
+ (void)closeSession:(nullable OCAppGatewaySession *)session NS_SWIFT_NAME(close(_:));

/// Install the networking hooks once; later calls do nothing.
+ (void)install;

/// The gateway request for `request`, or nil when it is not for an app backend.
+ (nullable NSURLRequest *)routedRequest:(NSURLRequest *)request;

@end

/// Performs app backend requests of sessions that list it (`expo/fetch`).
NS_SWIFT_NAME(AppGatewayURLProtocol)
@interface OCAppGatewayURLProtocol : NSURLProtocol
@end

NS_ASSUME_NONNULL_END
