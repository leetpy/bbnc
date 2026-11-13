#import <Foundation/Foundation.h>
#import <Vision/Vision.h>
#import <ImageIO/ImageIO.h>

// 使用 macOS Vision 本地识别截图，输出原图像素坐标；不发送图片到外部服务。
int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if (argc != 2) return 2;
        NSURL *url = [NSURL fileURLWithPath:[NSString stringWithUTF8String:argv[1]]];
        CGImageSourceRef source = CGImageSourceCreateWithURL((__bridge CFURLRef)url, NULL);
        if (!source) return 3;
        CGImageRef image = CGImageSourceCreateImageAtIndex(source, 0, NULL);
        CFRelease(source);
        if (!image) return 3;
        double width = CGImageGetWidth(image), height = CGImageGetHeight(image);
        VNRecognizeTextRequest *request = [[VNRecognizeTextRequest alloc] init];
        request.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
        request.recognitionLanguages = @[@"zh-Hans", @"en-US"];
        request.usesLanguageCorrection = NO;
        VNImageRequestHandler *handler = [[VNImageRequestHandler alloc] initWithCGImage:image options:@{}];
        NSError *error = nil;
        BOOL ok = [handler performRequests:@[request] error:&error];
        CGImageRelease(image);
        if (!ok) { fprintf(stderr, "%s\n", error.localizedDescription.UTF8String); return 4; }
        NSMutableArray *rows = [NSMutableArray array];
        for (VNRecognizedTextObservation *observation in request.results) {
            VNRecognizedText *text = [observation topCandidates:1].firstObject;
            if (!text) continue;
            CGRect box = observation.boundingBox;
            [rows addObject:@{@"text": text.string, @"confidence": @(text.confidence),
                @"bounds": @[@((int)(box.origin.x * width)),
                    @((int)((1 - CGRectGetMaxY(box)) * height)),
                    @((int)(CGRectGetMaxX(box) * width)),
                    @((int)((1 - box.origin.y) * height))]}];
        }
        NSData *data = [NSJSONSerialization dataWithJSONObject:
            @{@"width": @(width), @"height": @(height), @"rows": rows} options:0 error:&error];
        if (!data) return 5;
        puts([[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding].UTF8String);
    }
    return 0;
}
