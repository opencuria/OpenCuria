// Copyright OpenCuria contributors. AGPL-3.0-only.

#import "AppGateway.h"

#import <React/RCTHTTPRequestHandler.h>
#import <React/RCTWebSocketModule.h>

static NSString *const OCSessionHeader = @"X-OpenCuria-App-Session";
static NSString *const OCRoutedKey = @"OCAppGatewayRouted";

@interface OCAppGatewaySession ()
- (instancetype)initWithURL:(NSURL *)url token:(NSString *)token domain:(NSString *)domain;
@end

@implementation OCAppGatewaySession

- (instancetype)initWithURL:(NSURL *)url token:(NSString *)token domain:(NSString *)domain
{
  if ((self = [super init])) {
    _url = url;
    _token = [token copy];
    _domain = [domain copy];
  }
  return self;
}

@end

static OCAppGatewaySession *OCCurrentSession; // Guarded by @synchronized(OCAppGateway.class).

static BOOL OCIsLabel(NSString *value)
{
  static NSRegularExpression *pattern;
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    pattern = [NSRegularExpression regularExpressionWithPattern:@"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
                                                        options:0
                                                          error:nil];
  });
  return [pattern numberOfMatchesInString:value options:0 range:NSMakeRange(0, value.length)] == 1;
}

@implementation OCAppGateway

+ (nullable OCAppGatewaySession *)current
{
  @synchronized(self) {
    return OCCurrentSession;
  }
}

+ (nullable OCAppGatewaySession *)sessionFromJSON:(nullable NSDictionary *)json
{
  if (![json isKindOfClass:NSDictionary.class]) {
    return nil;
  }
  id rawURL = json[@"url"], rawToken = json[@"token"], rawDomain = json[@"domain"];
  if (![rawURL isKindOfClass:NSString.class] || ![rawToken isKindOfClass:NSString.class] ||
      ![rawDomain isKindOfClass:NSString.class]) {
    return nil;
  }
  NSString *token = rawToken, *domain = [(NSString *)rawDomain lowercaseString];
  NSURL *url = [NSURL URLWithString:rawURL];
  NSString *scheme = url.scheme.lowercaseString;
  if (token.length == 0 || domain.length == 0 || url.host.length == 0 ||
      !([scheme isEqualToString:@"https"] || [scheme isEqualToString:@"http"])) {
    return nil;
  }
  return [[OCAppGatewaySession alloc] initWithURL:url token:token domain:domain];
}

+ (void)openSession:(nullable OCAppGatewaySession *)session
{
  @synchronized(self) {
    OCCurrentSession = session;
  }
}

+ (void)closeSession:(nullable OCAppGatewaySession *)session
{
  @synchronized(self) {
    if (session != nil && OCCurrentSession == session) {
      OCCurrentSession = nil;
    }
  }
}

+ (nullable NSURLRequest *)routedRequest:(NSURLRequest *)request
{
  OCAppGatewaySession *session = [self current];
  NSURL *url = request.URL;
  NSString *host = url.host.lowercaseString;
  if (session == nil || host == nil) {
    return nil;
  }
  NSString *suffix = [@"." stringByAppendingString:session.domain];
  if (![host hasSuffix:suffix]) {
    return nil;
  }
  NSString *label = [host substringToIndex:host.length - suffix.length];
  if (!OCIsLabel(label)) {
    return nil;
  }
  NSString *scheme = url.scheme.lowercaseString;
  BOOL secure = [scheme isEqualToString:@"https"] || [scheme isEqualToString:@"wss"];
  NSInteger port = url.port != nil ? url.port.integerValue : (secure ? 443 : 80);
  NSURLComponents *source = [NSURLComponents componentsWithURL:url resolvingAgainstBaseURL:NO];
  NSURLComponents *target = [NSURLComponents componentsWithURL:session.url resolvingAgainstBaseURL:NO];
  NSString *base = target.percentEncodedPath ?: @"";
  if ([base hasSuffix:@"/"]) {
    base = [base substringToIndex:base.length - 1];
  }
  NSString *path = source.percentEncodedPath.length > 0 ? source.percentEncodedPath : @"/";
  target.percentEncodedPath = [NSString stringWithFormat:@"%@/%@/%ld%@", base, label, (long)port, path];
  target.percentEncodedQuery = source.percentEncodedQuery;
  NSURL *routedURL = target.URL;
  if (routedURL == nil) {
    return nil;
  }
  NSMutableURLRequest *routed = [request mutableCopy];
  routed.URL = routedURL;
  [routed setValue:session.token forHTTPHeaderField:OCSessionHeader];
  return routed;
}

+ (void)install
{
  static dispatch_once_t once;
  dispatch_once(&once, ^{
    // fetch, XMLHttpRequest and React Native images.
    RCTSetCustomHTTPRequestInterceptor(^NSURLRequest *(NSURLRequest *request) {
      return [OCAppGateway routedRequest:request];
    });
    // React Native passes its own mutable request and, when the provider
    // returns nil, opens the socket itself with the app's subprotocols.
    RCTSetCustomSRWebSocketProvider(^SRWebSocket *(NSURLRequest *request) {
      NSURLRequest *routed = [OCAppGateway routedRequest:request];
      if (routed != nil && [request isKindOfClass:NSMutableURLRequest.class]) {
        NSMutableURLRequest *mutable = (NSMutableURLRequest *)request;
        mutable.URL = routed.URL;
        [mutable setValue:[routed valueForHTTPHeaderField:OCSessionHeader] forHTTPHeaderField:OCSessionHeader];
        // Cookies the backend set through the gateway belong to the gateway URL.
        NSArray<NSHTTPCookie *> *cookies = [NSHTTPCookieStorage.sharedHTTPCookieStorage cookiesForURL:routed.URL];
        [mutable setValue:[NSHTTPCookie requestHeaderFieldsWithCookies:cookies][@"Cookie"]
            forHTTPHeaderField:@"Cookie"];
      }
      SRWebSocket *useDefault = nil;
      return useDefault;
    });
  });
}

@end

@interface OCAppGatewayURLProtocol () <NSURLSessionDataDelegate>
@end

@implementation OCAppGatewayURLProtocol {
  NSURLSession *_session;
}

+ (BOOL)canInitWithRequest:(NSURLRequest *)request
{
  return [NSURLProtocol propertyForKey:OCRoutedKey inRequest:request] == nil &&
      [OCAppGateway routedRequest:request] != nil;
}

+ (NSURLRequest *)canonicalRequestForRequest:(NSURLRequest *)request
{
  return request;
}

- (void)startLoading
{
  NSMutableURLRequest *routed = [[OCAppGateway routedRequest:self.request] mutableCopy];
  if (routed == nil) {
    [self.client URLProtocol:self
            didFailWithError:[NSError errorWithDomain:NSURLErrorDomain code:NSURLErrorNotConnectedToInternet userInfo:nil]];
    return;
  }
  [NSURLProtocol setProperty:@YES forKey:OCRoutedKey inRequest:routed];
  NSURLSessionConfiguration *config = NSURLSessionConfiguration.defaultSessionConfiguration;
  config.HTTPCookieStorage = NSHTTPCookieStorage.sharedHTTPCookieStorage;
  config.HTTPShouldSetCookies = YES;
  config.HTTPCookieAcceptPolicy = NSHTTPCookieAcceptPolicyAlways;
  config.requestCachePolicy = NSURLRequestReloadIgnoringLocalCacheData;
  _session = [NSURLSession sessionWithConfiguration:config delegate:self delegateQueue:nil];
  [[_session dataTaskWithRequest:routed] resume];
}

- (void)stopLoading
{
  [_session invalidateAndCancel];
  _session = nil;
}

- (void)URLSession:(NSURLSession *)session
              dataTask:(NSURLSessionDataTask *)dataTask
    didReceiveResponse:(NSURLResponse *)response
     completionHandler:(void (^)(NSURLSessionResponseDisposition))completionHandler
{
  [self.client URLProtocol:self didReceiveResponse:response cacheStoragePolicy:NSURLCacheStorageNotAllowed];
  completionHandler(NSURLSessionResponseAllow);
}

- (void)URLSession:(NSURLSession *)session dataTask:(NSURLSessionDataTask *)dataTask didReceiveData:(NSData *)data
{
  // Streams chunk by chunk, so server-sent events arrive as they are sent.
  [self.client URLProtocol:self didLoadData:data];
}

- (void)URLSession:(NSURLSession *)session task:(NSURLSessionTask *)task didCompleteWithError:(NSError *)error
{
  if (error != nil) {
    [self.client URLProtocol:self didFailWithError:error];
  } else {
    [self.client URLProtocolDidFinishLoading:self];
  }
  [session finishTasksAndInvalidate];
}

@end
